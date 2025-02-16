import json
import os
import logging
import collections
import tensorflow as tf
import numpy as np
from nltk.tokenize import word_tokenize

class SequenceTagger:
    def __init__(self, hparams, data_dir):

        self.hparams = hparams
        self.data_dir = data_dir
        self._dropout_keep_prob_ph = tf.placeholder(tf.float32, shape=[], name="dropout_keep_prob")
        self._logger = logging.getLogger(__name__)
        self.iterator_initializers = []
        self.idx = 0

        with open(os.path.join(data_dir, "train.inputs"), "r") as _f_handle:
            self._inputs = [l.strip() for l in list(_f_handle) if len(l.strip()) > 0]
        with open(os.path.join(data_dir, "train.labels"), "r") as _f_handle:
            self._labels = [l.strip() for l in list(_f_handle) if len(l.strip()) > 0]

        """
        [A]
        Vocabulary(단어집) 파일을 로드합니다.
        단어 -> id, id -> 단어 변환 테이블을 생성합니다.
        """

        with open(os.path.join(data_dir, "train.vocab"), "r") as _f_handle:
            vocab = [l.strip() for l in list(_f_handle) if len(l.strip()) > 0]
            #strip() = 한 시퀀스의 양 끝 공백 제거

        #vocab_size 보다 len(vocab) 길면 vocab_size 만큼만 vocab에 넣음
        if len(vocab) > hparams.vocab_size:
            vocab = vocab[:hparams.vocab_size]
        vocab.insert(len(vocab), '<UNK>')
        self.id2word = vocab    # 리스트 : 인덱싱에 유용
        self.word2id = {}       # {} = 딕셔너리 생성 <- 딕셔너리는 해쉬테이블을 사용 => Time complexity Big O(1)임
        for i, word in enumerate(vocab):
            self.word2id[word] = i

        #BIO(Beginning Inside Outside)
        #단어 -> id 생성 과정

        """
        [B]
        Label(태그 모음) 파일을 로드합니다.
        태그 -> id, id -> 태그 변환 테이블을 생성합니다.
        """

        with open(os.path.join(data_dir, "label.vocab"), "r") as _f_handle:
            labels = [l.strip() for l in list(_f_handle) if len(l.strip()) > 0] #위와 동일

        labels.insert(0, "PAD")         #단어의 0번째 자리도 PAD이므로 똑같이 맞춰줌
        self.id2label = labels
        self.label2id = {}              #딕셔너리 생성
        for i, label in enumerate(labels):
            self.label2id[label] = i

    def _inference(self, inputs:tf.Tensor, lengths:tf.Tensor):
        print("Building graph for model: sequence tagger")

        """
        [C][C]
        단어 임베딩 행렬을 생성합니다.
        단어 id를 단어 임베딩 텐서로 변환합니다.
        """
        output_dim = len(self.id2label)
        vocab_size = len(self.id2word) + 1
        embeddings = tf.get_variable(
            "embeddings",
            shape=[vocab_size, self.hparams.embedding_dim],
            initializer=tf.initializers.variance_scaling(
                scale=1.0, mode="fan_out", distribution="uniform")
        )

        embedded = tf.nn.embedding_lookup(embeddings, inputs)
        layer_out = embedded

        """
        [D][D]
        단어 임베딩을 RNN의 입력으로 사용하기 전,
        차원 수를 맞춰주고 성능을 향상시키기 위해
        projection layer를 생성하여 텐서를 통과시킵니다.
        (high level)
        """

        # shape dim 100 -> 128, (?, ?, 128)
        layer_out = tf.layers.dense(
            inputs=layer_out,
            units=self.hparams.rnn_hidden_dim,
            activation=tf.nn.relu,
            kernel_initializer=tf.initializers.variance_scaling(
                scale=1.0, mode="fan_avg", distribution="normal"),
            name="input_projection"
        )

        """
        [E]
        양방향 RNN을 생성하고, 여기에 텐서를 통과시킵니다.
        이렇게 하여, 단어간 의존 관계가 반영된 단어 자질 텐서를 얻습니다.
        """
        with tf.variable_scope("bi-RNN"):
            # Build RNN layers
            rnn_cell_forward = tf.contrib.rnn.GRUCell(self.hparams.rnn_hidden_dim)
            rnn_cell_backward = tf.contrib.rnn.GRUCell(self.hparams.rnn_hidden_dim)

            # Apply dropout to RNN
            if self.hparams.dropout_keep_prob < 1.0:
                rnn_cell_forward = tf.contrib.rnn.DropoutWrapper(rnn_cell_forward,
                                                                 output_keep_prob=self._dropout_keep_prob_ph)
                rnn_cell_backward = tf.contrib.rnn.DropoutWrapper(rnn_cell_backward,
                                                                  output_keep_prob=self._dropout_keep_prob_ph)


            # Stack multiple layers of RNN
            rnn_cell_forward = tf.contrib.rnn.MultiRNNCell([rnn_cell_forward] * self.hparams.rnn_depth)
            rnn_cell_backward = tf.contrib.rnn.MultiRNNCell([rnn_cell_backward] * self.hparams.rnn_depth)

            #tf.nn.bidirectional_dynamic_rnn
            (output_forward, output_backward), _ = tf.nn.bidirectional_dynamic_rnn(
                rnn_cell_forward, rnn_cell_backward,
                inputs=layer_out,
                sequence_length=lengths,
                dtype=tf.float32
            )

            hiddens = tf.concat([output_forward, output_backward], axis=-1)
            # shape = [batch_size, time, rnn_dim*2]

        """
        [F]
        마스킹을 적용하여 문장 길이를 통일하기 위해 적용했던 padding을 제거합니다. # 마스킹 => 패딩제거
        """

        mask = tf.sequence_mask(lengths)
        bi_lstm_out = tf.reshape(tf.boolean_mask(hiddens, mask), [-1, self.hparams.rnn_hidden_dim * 2])
        layer_out = bi_lstm_out

        # shape=[sum of seq length, 2*LSTM hidden layer size]

        """
        [G]
        단어 자질 텐서를 바탕으로 단어의 태그를 예측합니다.
        이를 위해 fully-connected(dense) layer를 생성하고 텐서를 통과시킵니다.
        """

        #W * X + b연산 (low level)
        with tf.variable_scope("read-out"):
            prev_layer_size = layer_out.get_shape().as_list()[-1]

            weight = tf.get_variable("weight", shape=[prev_layer_size, output_dim], #output_dim = 태그 개수
                                     initializer=tf.initializers.variance_scaling(
                                         scale=2.0, mode="fan_in", distribution="normal"
                                     ))
            bias = tf.get_variable("bias", shape=[output_dim],
                                   initializer=tf.initializers.zeros())
            predictions = tf.add(tf.matmul(layer_out, weight), bias, name='predictions')

        return predictions

    def _minibatch(self, _inputs, _labels):

        x_batch = []
        y_batch = []
        for i in range(self.hparams.batch_size):
            if self.idx + i >= len(_inputs): break
            x_batch.append(_inputs[self.idx + i])
            y_batch.append(_labels[self.idx + i])
        self.idx += self.hparams.batch_size
        return x_batch, y_batch


    def _load_data(self, inputs, labels=None):

        x_batch = []
        y_batch = []
        word_arr = []
        label_arr = []
        label_len = []

        # 전체 inputs, labels를 배치단위로 분할반복

        if labels is not None:
            x_batch, y_batch = self._minibatch(inputs, labels)
        tokenized_inputs = []       # word_list
        tokenized_labels = []       # label_list
        _length = []                # length

        for i in range(len(x_batch)):
            sentence = word_tokenize(x_batch[i])
            tokenized_inputs.append(sentence)
            _labels = word_tokenize(y_batch[i])
            tokenized_labels.append(_labels)
            _length.append(len(sentence))
            word_arr = np.zeros([len(_length), max(_length)], dtype=np.int32)
            label_arr = np.zeros([len(_length), max(_length)], dtype=np.int32)

        # word2id
        for sent_word, sentence in enumerate(tokenized_inputs):
            for word_id, word in enumerate(sentence):
                if word in self.word2id:
                    word_arr[sent_word][word_id] = self.word2id[word]
                else:
                    word_arr[sent_word][word_id] = self.word2id['<UNK>']  # OOV

        # label2id
        for sent_id, sentence in enumerate(tokenized_labels):
            for label_id, word in enumerate(sentence):
                if word in self.label2id:
                    label_arr[sent_id][label_id] = self.label2id[word]

        # label 2->1 dim
        for i, label in enumerate(label_arr):
            for j in range(_length[i]):
                label_len.append(label_arr[i][j])

        return word_arr, label_len, _length

    def build_placeholders(self):
        self.inputs_ph = tf.placeholder(tf.int32, shape=[None, None], name="inputs_ph")
        self.labels_ph = tf.placeholder(tf.int32, shape=[None], name="labels_ph")
        self.lengths_ph = tf.placeholder(tf.int32, shape=[None], name="lengths_ph")

    def train(self):
        sess = tf.Session()

        _inputs = self._inputs
        _labels = self._labels

        with sess.as_default():
            global_step = tf.Variable(0, name='global_step', trainable=False)
            self.build_placeholders()

            with tf.variable_scope("inference", reuse=False):
                logits = self._inference(self.inputs_ph, self.lengths_ph)

            """
            [O]
            모델을 훈련시키기 위해 필요한 오퍼레이션들을 텐서 그래프에 추가합니다.
            여기에는 loss, train, accuracy 계산 등이 포함됩니다.
            """
            loss_op = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=self.labels_ph,
                                                                     name="cross_entropy")
            loss_op = tf.reduce_mean(loss_op, name='cross_entropy_mean')
            train_op = tf.train.AdamOptimizer().minimize(loss_op, global_step=global_step)

            eval = tf.nn.in_top_k(logits, self.labels_ph, 1)
            correct_count = tf.reduce_sum(tf.cast(eval, tf.int32))
            accuracy = tf.divide(correct_count, tf.shape(self.labels_ph)[0])

            # Initialize variables.

            tf.global_variables_initializer().run()
            saver = tf.train.Saver()

            for epochs_completed in range(self.hparams.num_epochs):
                for i in range(int(len(_inputs) / self.hparams.batch_size) + 1):

                    """
                    [P]
                    그래프에 데이터를 입력하여 필요한 계산들을 수행하고,
                    Loss에 따라 gradient를 계산하여 파라미터들을 업데이트합니다.
                    이러한 과정을 training step이라고 합니다.
                    """

                    inputs, labels, lengths = self._load_data(_inputs, _labels)  # load_data = 인풋, 정답, 문장 별 길이 텐서를 만들어 줌

                    try:
                        accuracy_val, loss_val, global_step_val, _ = sess.run(
                            [accuracy, loss_op, global_step, train_op],
                            feed_dict={self._dropout_keep_prob_ph: self.hparams.dropout_keep_prob,
                                       self.inputs_ph: inputs,
                                       self.labels_ph: labels,
                                       self.lengths_ph: lengths})

                        if global_step_val % 10 == 0:
                            print("[Step %d] loss: %.4f, accuracy: %.2f%%" % (
                            global_step_val, loss_val, accuracy_val * 100))
                    except tf.errors.OutOfRangeError:
                        # End of epoch.
                        break
                self.idx = 0

                """
                [Q]
                전체 학습 데이터에 대하여 1회 학습을 완료하였습니다.
                이를 1 epoch라고 합니다.
                딥러닝 모델의 학습은 일반적으로 수십~수백 epoch 동안 진행됩니다.
                """
                self._logger.info("End of epoch %d." % (epochs_completed + 1))
                save_path = saver.save(sess, "saves/model.ckpt", global_step=global_step_val)
                self._logger.info("Model saved at: %s" % save_path)

    def predict(self, saved_file:str):
        sentence = input("Enter a sentence: ")

        """
        [H]
        입력 문자열을 단어/문장부호 단위로 쪼개고, 이를 다시 단어 id로 변환합니다.
        """
        sentence = word_tokenize(sentence)
        word_ids = []
        for word in sentence:
            if word in self.word2id:
                word_ids.append(self.word2id[word])
            else:
                word_ids.append(len(self.word2id))

        sess = tf.Session()

        with sess.as_default():
            """
            [I]
            태깅을 수행하기 위해 텐서 그래프를 생성합니다.  python -> tensor
            """
            dense_word_ids = tf.constant(word_ids)
            lengths = tf.constant(len(word_ids))

            # Insert batch dimension.
            dense_word_ids = tf.expand_dims(dense_word_ids, axis=0)
            lengths = tf.expand_dims(lengths, axis=0)

            with tf.variable_scope("inference", reuse=False):
                logits = self._inference(dense_word_ids, lengths)
            predictions = tf.argmax(logits, axis=1)

            """
            [J]
            저장된 모델을 로드하고, 데이터를 입력하여 태깅 결과를 얻습니다.
            """
            saver = tf.train.Saver()
            saver.restore(sess, saved_file)
            pred_val = sess.run(
                [predictions],
                feed_dict={self._dropout_keep_prob_ph: 1.0}
            )[0]

        """
        [K]
        태깅 결과를 출력합니다.
        """
        pred_str = [self.id2label[i] for i in pred_val]
        for word, tag in zip(sentence, pred_str):
            print("%s[%s]" % (word, tag), end=' ')

def train_model(builder_class):

    with open("default.json", "r") as f_handle:
        hparams_dict = json.load(f_handle)

    hparams = collections.namedtuple("HParams", sorted(hparams_dict.keys()))(**hparams_dict)

    data_dir = "CoNLL-2003"
    # Build graph
    model = builder_class(hparams, data_dir)
    model.train()

def load_and_predict(builder_class):

    with open("default.json", "r") as f_handle:
        hparams_dict = json.load(f_handle)

    hparams = collections.namedtuple("HParams", sorted(hparams_dict.keys()))(**hparams_dict)

    data_dir = "CoNLL-2003"

    saved_dir = "saves/model.ckpt-1876"
    model = builder_class(hparams, data_dir)
    model.predict(saved_dir)


if __name__ == "__main__":
    trainable = False
    if trainable == True:
        train_model(SequenceTagger)
    else:
        load_and_predict(SequenceTagger)
