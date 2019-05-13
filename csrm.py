# coding=utf-8

from __future__ import print_function
import numpy as np
import tensorflow as tf
from time import time
from ome import OME
import data_process
import time
import pickle
tf.set_random_seed(42)
np.random.seed(42)

def numpy_floatX(data):
    return np.asarray(data, dtype=np.float32)

class CSRM:
    def __init__(self,
                 sess,
                 n_items,
                 dim_proj,
                 hidden_units,
                 patience,
                 memory_size,
                 memory_dim,
                 shift_range,
                 controller_layer_numbers,
                 batch_size,
                 epoch,
                 lr,
                 keep_probability,
                 no_dropout,
                 display_frequency,
                 ):
        self.sess = sess
        self.n_items = n_items
        self.dim_proj = dim_proj
        self.hidden_units = hidden_units
        self.patience = patience
        self.memory_size = memory_size
        self.memory_dim = memory_dim
        self.shift_range = shift_range
        self.controller_layer_numbers = controller_layer_numbers
        self.batch_size = batch_size
        self.epoch = epoch
        self.lr = lr
        self.keep_probability = np.array([0.75, 0.5])
        self.no_dropout = np.array([1.0, 1.0])
        self.display_frequency = display_frequency
        self.controller_hidden_layer_size = 100
        self.controller_output_size = self.memory_dim + 1 + 1 + (self.shift_range * 2 + 1) + 1 + self.memory_dim * 3 + 1 + 1 + (self.shift_range * 2 + 1) + 1

        self.train_loss_record = []
        self.valid_loss_record = []
        self.test_loss_record = []

        self.train_recall_record, self.train_mrr_record = [], []
        self.valid_recall_record, self.valid_mrr_record = [], []
        self.test_recall_record, self.test_mrr_record = [], []

        self.build_graph()


    def build_graph(self):
        self.params = self.init_params()

        self.x_input = tf.placeholder(tf.int64, [None, None])
        self.mask_x = tf.placeholder(tf.float32, [None, None])
        self.y_target = tf.placeholder(tf.int64, [None])
        self.len_x = tf.placeholder(tf.int64, [None])
        self.keep_prob = tf.placeholder(tf.float32, [None])
        self.starting = tf.placeholder(tf.bool)

        """       
        attention gru & global gru 实现
        Output:
        global_session_representation
        attentive_session_represention
        """
        self.n_timesteps = tf.shape(self.x_input)[1]
        self.n_samples = tf.shape(self.x_input)[0]

        emb = tf.nn.embedding_lookup(self.params['Wemb'], self.x_input)
        emb = tf.nn.dropout(emb, keep_prob=self.keep_prob[0])

        with tf.variable_scope('global_encoder'):
            cell_global = tf.nn.rnn_cell.GRUCell(self.hidden_units)
            init_state = cell_global.zero_state(self.n_samples, tf.float32)
            outputs_global, state_global = tf.nn.dynamic_rnn(cell_global, inputs=emb, sequence_length=self.len_x,
                                                             initial_state=init_state, dtype=tf.float32)
            last_global = state_global  # batch_size*hidden_units

        with tf.variable_scope('local_encoder'):
            cell_local = tf.nn.rnn_cell.GRUCell(self.hidden_units)
            init_statel = cell_local.zero_state(self.n_samples, tf.float32)
            outputs_local, state_local = tf.nn.dynamic_rnn(cell_local, inputs=emb, sequence_length=self.len_x,
                                                           initial_state=init_statel, dtype=tf.float32)
            last_h = state_local  # batch_size*hidden_units

            tmp_0 = tf.reshape(outputs_local, [-1, self.hidden_units])
            tmp_1 = tf.reshape(tf.matmul(tmp_0, self.params['W_encoder']),
                               [self.n_samples, self.n_timesteps, self.hidden_units])
            tmp_2 = tf.expand_dims(tf.matmul(last_h, self.params['W_decoder']), 1)  # batch_size*hidden_units
            tmp_3 = tf.reshape(tf.sigmoid(tmp_1 + tmp_2), [-1, self.hidden_units])  # batch_size,n_steps, hidden_units
            alpha = tf.matmul(tmp_3, tf.transpose(self.params['bl_vector']))
            res = tf.reduce_sum(alpha, axis=1)
            sim_matrix = tf.reshape(res, [self.n_samples, self.n_timesteps])

            att = tf.nn.softmax(sim_matrix * self.mask_x) * self.mask_x  # batch_size*n_step
            p = tf.expand_dims(tf.reduce_sum(att, axis=1), 1)
            weight = att / p
            atttention_proj = tf.reduce_sum((outputs_local * tf.expand_dims(weight, 2)), 1)
        self.global_session_representation = last_global
        self.attentive_session_represention = atttention_proj

        # 初始化ntm_cell，用于读写memory
        self.ome_cell = OME(mem_size=(self.memory_size, self.memory_dim), shift_range=self.shift_range,
                            hidden_units=self.hidden_units)

        # 创建用于存放读写memory的state的placeholder
        self.state = tf.placeholder(dtype=tf.float32, shape=[None, self.hidden_units])
        self.memory_network_reads, self.memory_new_state = self.ome_cell(self.state, atttention_proj, self.starting)

        att_mean, att_var = tf.nn.moments(self.attentive_session_represention, axes=[1])
        self.attentive_session_represention = (self.attentive_session_represention - tf.expand_dims(att_mean, 1)) / tf.expand_dims(tf.sqrt(att_var + 1e-10), 1)
        glo_mean, glo_var = tf.nn.moments(self.global_session_representation, axes=[1])
        self.global_session_representation = (self.global_session_representation - tf.expand_dims(glo_mean, 1)) / tf.expand_dims(tf.sqrt(glo_var + 1e-10), 1)
        ntm_mean, ntm_var = tf.nn.moments(self.memory_network_reads, axes=[1])
        self.memory_network_reads = (self.memory_network_reads - tf.expand_dims(ntm_mean, 1)) / tf.expand_dims(tf.sqrt(ntm_var + 1e-10), 1)

        new_gate = tf.matmul(self.attentive_session_represention, self.params['inner_encoder']) + \
                   tf.matmul(self.memory_network_reads, self.params['outer_encoder']) + \
                   tf.matmul(self.global_session_representation, self.params['state_encoder'])
        new_gate = tf.nn.sigmoid(new_gate)
        self.narm_representation = tf.concat((self.attentive_session_represention, self.global_session_representation), axis=1)
        self.memory_representation = tf.concat((self.memory_network_reads, self.memory_network_reads), axis=1)
        final_representation = new_gate * self.narm_representation + (1 - new_gate) * self.memory_representation

        # prediction
        proj = tf.nn.dropout(final_representation, keep_prob=self.keep_prob[1])
        ytem = tf.matmul(self.params['Wemb'], self.params['bili'])   # [n_items, 200]
        hypothesis = tf.matmul(proj, tf.transpose(ytem)) + 1e-10 # [batch_size, n_step, n_items]
        self.hypo = tf.nn.softmax(hypothesis)
        self.loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(logits=hypothesis, labels=self.y_target))
        # optimize
        self.optimizer = tf.train.AdamOptimizer(learning_rate=self.lr).minimize(self.loss)

        self.saver = tf.train.Saver(max_to_keep=1)

    def init_weights(self, i_name, shape):
        sigma = np.sqrt(2. / shape[0])
        return tf.get_variable(name=i_name, dtype=tf.float32, initializer=tf.random_normal(shape) * sigma)

    def init_params(self):
        """
        Global (not GRU) parameter. For the embeding and the classifier.
        """
        params = dict()
        # embedding
        params['Wemb'] = self.init_weights('Wemb', (self.n_items, self.dim_proj))
        # attention
        params['W_encoder'] = self.init_weights('W_encoder', (self.hidden_units, self.hidden_units))
        params['W_decoder'] = self.init_weights('W_decoder', (self.hidden_units, self.hidden_units))
        params['bl_vector'] = self.init_weights('bl_vector', (1, self.hidden_units))
        # classifier
        params['bili'] = self.init_weights('bili', (self.dim_proj, 2 * self.hidden_units))
        # final gate
        params['inner_encoder'] = self.init_weights('inner_encoder', (self.hidden_units, 1))
        params['outer_encoder'] = self.init_weights('outer_encoder', (self.hidden_units, 1))
        params['state_encoder'] = self.init_weights('state_encoder', (self.hidden_units, 1))

        return params

    def get_minibatches_idx(self, n, minibatch_size, shuffle=False):
        """
        Used to shuffle the dataset at each iteration.
        """
        idx_list = np.arange(n, dtype="int32")

        if shuffle:
            np.random.shuffle(idx_list)

        minibatches = []
        minibatch_start = 0
        for i in range(n // minibatch_size):
            minibatches.append(idx_list[minibatch_start:  minibatch_start + minibatch_size])
            minibatch_start += minibatch_size

        if minibatch_start != n:
            # Make a minibatch out of what is left
            minibatches.append(idx_list[minibatch_start:])

        return zip(range(len(minibatches)), minibatches)

    def pred_evaluation(self, data, iterator, ntm_init_state):
        """
        Compute recall@20 and mrr@20
        f_pred_prob: Theano fct computing the prediction
        prepare_data: usual prepare_data for that dataset.
        """
        recall = 0.0
        mrr = 0.0
        evalutation_point_count = 0

        for _, valid_index in iterator:
            batch_data = [data[0][t] for t in valid_index]
            batch_label = [data[1][t] for t in valid_index]
            feed_dict = self.construct_feeddict(batch_data, batch_label, self.no_dropout, ntm_init_state)
            pred, ntm_init_state = self.sess.run([self.hypo, self.memory_new_state], feed_dict=feed_dict)
            ranks = (pred.T > np.diag(pred.T[batch_label])).sum(axis=0) + 1  # np.diag(preds.T[targets]) each bacth target"s score
            rank_ok = (ranks <= 20)  # tf.diag_part(input, name=None)
            recall += rank_ok.sum()
            mrr += (1.0 / ranks[rank_ok]).sum()
            evalutation_point_count += len(ranks)

        recall = numpy_floatX(recall) / evalutation_point_count
        mrr = numpy_floatX(mrr) / evalutation_point_count
        eval_score = (recall, mrr)

        return eval_score, ntm_init_state


    def construct_feeddict(self, batch_data, batch_label, keepprob, state, starting=False):
        x, mask, y, lengths = data_process.prepare_data(batch_data, batch_label)
        feed = {self.x_input: x, self.mask_x: mask, self.y_target: y, self.len_x: lengths, self.keep_prob: keepprob,
                self.state: state, self.starting: starting}
        # feed the initialized state into placeholder

        return feed


    def train(self, Train_data, Validation_data, Test_data, result_path='save/'):

        # 初始化参数
        print(" [*] Initialize all variables")
        self.sess.run(tf.global_variables_initializer())
        print(" [*] Initialization finished")

        kf = self.get_minibatches_idx(len(Train_data[0]), self.batch_size)
        kf_valid = self.get_minibatches_idx(len(Validation_data[0]), self.batch_size)
        kf_test = self.get_minibatches_idx(len(Test_data[0]), self.batch_size)

        uidx = 0
        bad_count = 0
        estop = False
        for epoch in range(self.epoch):
            start_time = time.time()
            nsamples = 0
            epoch_loss = []
            session_memory_state = np.random.normal(0, 0.05, size=[1, self.hidden_units])
            starting = True
            # 训练
            print('*****************************************************************')
            for _, train_index in kf:
                uidx += 1
                # Select the random examples for this minibatch
                batch_label = [Train_data[1][t] for t in train_index]
                batch_data = [Train_data[0][t] for t in train_index]
                nsamples += len(batch_label)
                feed_dict = self.construct_feeddict(batch_data, batch_label, self.keep_probability, session_memory_state, starting)
                cost, _, session_memory_state = self.sess.run([self.loss, self.optimizer, self.memory_new_state], feed_dict=feed_dict)
                starting = False

                epoch_loss.append(cost)
                if np.mod(uidx, self.display_frequency) == 0:
                    print('Epoch ', epoch, 'Update ', uidx, 'Loss ', np.mean(epoch_loss))

            valid_evaluation, session_memory_state = self.pred_evaluation(Validation_data, kf_valid, session_memory_state)
            self.valid_recall_record.append(valid_evaluation[0])
            self.valid_mrr_record.append(valid_evaluation[1])

            if valid_evaluation[0] >= np.array(self.valid_recall_record).max():
                bad_count = 0
                print('Best perfomance updated!')
                self.saver.save(self.sess, result_path + "/model.ckpt")
                pickle.dump(session_memory_state, open('save/lastfm_memory.pkl', 'w'))

            test_evaluation, session_memory_state = self.pred_evaluation(Test_data, kf_test, session_memory_state)
            self.test_recall_record.append(test_evaluation[0])
            self.test_mrr_record.append(test_evaluation[1])
            print('Valid Recall@20:', valid_evaluation[0], '   Valid Mrr@20:', valid_evaluation[1],
                  '\nTest Recall@20', test_evaluation[0], '   Test Mrr@20:', test_evaluation[1])

            if valid_evaluation[0] < np.array(self.valid_recall_record).max():
                bad_count += 1
                print('===========================>Bad counter: ' + str(bad_count))
                print('current validation recall: ' + str(valid_evaluation[0]) +
                      '      history max recall:' + str(np.array(self.valid_recall_record).max()))
                if bad_count >= self.patience:
                    print('Early Stop!')
                    estop = True
            end_time = time.time()
            print('Seen %d samples' % nsamples)
            print(('This epoch took %.1fs' % (end_time - start_time)))
            print('*****************************************************************')
            if estop:
                break

        p = self.valid_recall_record.index(np.array(self.valid_recall_record).max())
        print('=================Best performance=================')
        print('Valid Recall@20:', self.valid_recall_record[p], '   Valid Mrr@20:', self.valid_mrr_record[p],
                  '\nTest Recall@20', self.test_recall_record[p], '   Test Mrr@20:', self.test_mrr_record[p])
        print('==================================================')






