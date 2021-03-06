from btgym.algorithms.nn_utils import *
from btgym.algorithms.utils import *
import tensorflow as tf
from tensorflow.contrib.layers import flatten as batch_flatten

from btgym.algorithms import BaseAacPolicy

class StackedLstmPolicy(BaseAacPolicy):
    """
    Conv.-Stacked_LSTM policy, based on `NAV A3C agent` architecture from
    `LEARNING TO NAVIGATE IN COMPLEX ENVIRONMENTS` by Mirowski et all. and
    `LEARNING TO REINFORCEMENT LEARN` by JX Wang et all.

    Papers:
        https://arxiv.org/pdf/1611.03673.pdf
        https://arxiv.org/pdf/1611.05763.pdf
    """

    def __init__(self,
                 ob_space,
                 ac_space,
                 rp_sequence_size,
                 lstm_class=rnn.BasicLSTMCell,
                 lstm_layers=(64,256),
                 aux_estimate=False,
                 **kwargs):
        """
        Defines [partially shared] on/off-policy networks for estimating  action-logits, value function,
        reward and state 'pixel_change' predictions.
        Expects multi-modal observation as array of shape `ob_space`.

        Args:
            ob_space:           dictionary of observation state shapes
            ac_space:           discrete action space shape (length)
            rp_sequence_size:   reward prediction sample length
            lstm_class:         tf.nn.lstm class
            lstm_layers:        tuple of LSTM layers sizes
            aux_estimate:       (bool), if True - add auxiliary tasks estimations to self.callbacks dictionary.
            **kwargs            not used
        """
        # 1D plug-in:
        kwargs.update(
            dict(
                conv_2d_filter_size=[3, 1],
                conv_2d_stride=[2, 1],
                pc_estimator_stride=[2, 1],
                duell_pc_x_inner_shape=(6, 1, 32),  # [6,3,32] if swapping W-C dims
                duell_pc_filter_size=(4, 1),
                duell_pc_stride=(2, 1),
            )
        )

        self.ob_space = ob_space
        self.ac_space = ac_space
        self.rp_sequence_size = rp_sequence_size
        self.lstm_class = lstm_class
        self.lstm_layers = lstm_layers
        self.aux_estimate = aux_estimate
        self.callback = {}

        # Placeholders for obs. state input:
        self.on_state_in = nested_placeholders(ob_space, batch_dim=None, name='on_policy_state_in')
        self.off_state_in = nested_placeholders(ob_space, batch_dim=None, name='off_policy_state_in_pl')
        self.rp_state_in = nested_placeholders(ob_space, batch_dim=None, name='rp_state_in')

        # Placeholders for concatenated action [one-hot] and reward [scalar]:
        self.on_a_r_in = tf.placeholder(tf.float32, [None, ac_space + 1], name='on_policy_action_reward_in_pl')
        self.off_a_r_in = tf.placeholder(tf.float32, [None, ac_space + 1], name='off_policy_action_reward_in_pl')

        # Placeholders for rnn batch and time-step dimensions:
        self.on_batch_size = tf.placeholder(tf.int32, name='on_policy_batch_size')
        self.on_time_length = tf.placeholder(tf.int32, name='on_policy_sequence_size')

        self.off_batch_size = tf.placeholder(tf.int32, name='off_policy_batch_size')
        self.off_time_length = tf.placeholder(tf.int32, name='off_policy_sequence_size')

        # Base on-policy AAC network:
        # Conv. layers:
        on_aac_x = conv_2d_network(self.on_state_in['external'], ob_space['external'], ac_space, **kwargs)

        # Reshape rnn inputs for  batch training as [rnn_batch_dim, rnn_time_dim, flattened_depth]:
        x_shape_dynamic = tf.shape(on_aac_x)
        max_seq_len = tf.cast(x_shape_dynamic[0] / self.on_batch_size, tf.int32)
        x_shape_static = on_aac_x.get_shape().as_list()

        on_a_r_in = tf.reshape(self.on_a_r_in, [self.on_batch_size, max_seq_len, ac_space + 1])
        on_aac_x = tf.reshape( on_aac_x, [self.on_batch_size, max_seq_len, np.prod(x_shape_static[1:])])

        # Feed last action_reward into first LSTM layer along with encoded `external` state features:
        on_stage2_1_input = [on_aac_x, on_a_r_in]
        on_aac_x = tf.concat(on_stage2_1_input, axis=-1)

        # First LSTM layer takes concatenated encoded `external` state and last action_reward tensor:
        [on_x_lstm_1_out, self.on_lstm_1_init_state, self.on_lstm_1_state_out, self.on_lstm_1_state_pl_flatten] =\
            lstm_network(on_aac_x, self.on_time_length, lstm_class, (lstm_layers[0],), name='lstm_1')

        # Second LSTM layer takes concatenated encoded 'external' state, LSTM_1 output,
        # last_action_reward and `internal_state` tensors:
        on_stage2_2_input = [on_x_lstm_1_out] + on_stage2_1_input

        if 'internal' in list(self.on_state_in.keys()):
            x_int_shape_static = self.on_state_in['internal'].get_shape().as_list()
            x_int = tf.reshape(
                self.on_state_in['internal'],
                [self.on_batch_size, max_seq_len, np.prod(x_int_shape_static[1:])]
            )
            on_stage2_2_input.append(x_int)

        on_aac_x = tf.concat(on_stage2_2_input, axis=-1)

        [on_x_lstm_2_out, self.on_lstm_2_init_state, self.on_lstm_2_state_out, self.on_lstm_2_state_pl_flatten] = \
            lstm_network(on_aac_x, self.on_time_length, lstm_class, (lstm_layers[-1],), name='lstm_2')


        # Reshape back to [batch, flattened_depth], where batch = rnn_batch_dim * rnn_time_dim:
        x_shape_static = on_x_lstm_2_out.get_shape().as_list()
        on_x_lstm_out = tf.reshape(on_x_lstm_2_out, [x_shape_dynamic[0], x_shape_static[-1]])

        # Aac policy and value outputs and action-sampling function:
        [self.on_logits, self.on_vf, self.on_sample] = dense_aac_network(on_x_lstm_out, ac_space)

        # Concatenate LSTM placeholders, init. states and context:
        self.on_lstm_init_state = (self.on_lstm_1_init_state, self.on_lstm_2_init_state)
        self.on_lstm_state_out = (self.on_lstm_1_state_out, self.on_lstm_2_state_out)
        self.on_lstm_state_pl_flatten = self.on_lstm_1_state_pl_flatten + self.on_lstm_2_state_pl_flatten


        if False: # Temp. disable

            # Off-policy AAC network (shared):
            off_aac_x = conv_2d_network(self.off_state_in['external'], ob_space['external'], ac_space, reuse=True, **kwargs)

            # Reshape rnn inputs for  batch training as [rnn_batch_dim, rnn_time_dim, flattened_depth]:
            x_shape_dynamic = tf.shape(off_aac_x)
            max_seq_len = tf.cast(x_shape_dynamic[0] / self.off_batch_size, tf.int32)
            x_shape_static = off_aac_x.get_shape().as_list()

            off_a_r_in = tf.reshape(self.off_a_r_in, [self.off_batch_size, max_seq_len, ac_space + 1])
            off_aac_x = tf.reshape( off_aac_x, [self.off_batch_size, max_seq_len, np.prod(x_shape_static[1:])])

            off_stage2_input = [off_aac_x, off_a_r_in]

            if 'internal' in list(self.off_state_in.keys()):
                x_int_shape_static = self.off_state_in['internal'].get_shape().as_list()
                off_x_int = tf.reshape(
                    self.off_state_in['internal'],
                    [self.off_batch_size, max_seq_len, np.prod(x_int_shape_static[1:])]
                )
                off_stage2_input.append(off_x_int)

            off_aac_x = tf.concat(off_stage2_input, axis=-1)

            [off_x_lstm_out, _, _, self.off_lstm_state_pl_flatten] =\
                lstm_network(off_aac_x, self.off_time_length, lstm_class, lstm_layers, reuse=True)

            # Reshape back to [batch, flattened_depth], where batch = rnn_batch_dim * rnn_time_dim:
            x_shape_static = off_x_lstm_out.get_shape().as_list()
            off_x_lstm_out = tf.reshape(off_x_lstm_out, [x_shape_dynamic[0], x_shape_static[-1]])

            [self.off_logits, self.off_vf, _] =\
                dense_aac_network(off_x_lstm_out, ac_space, reuse=True)

            # Aux1: `Pixel control` network:
            # Define pixels-change estimation function:
            # Yes, it rather env-specific but for atari case it is handy to do it here, see self.get_pc_target():
            [self.pc_change_state_in, self.pc_change_last_state_in, self.pc_target] =\
                pixel_change_2d_estimator(ob_space['external'], **kwargs)

            self.pc_batch_size = self.off_batch_size
            self.pc_time_length = self.off_time_length

            self.pc_state_in = self.off_state_in
            self.pc_a_r_in = self.off_a_r_in
            self.pc_lstm_state_pl_flatten = self.off_lstm_state_pl_flatten

            # Shared conv and lstm nets, same off-policy batch:
            pc_x = off_x_lstm_out

            # PC duelling Q-network, outputs [None, 20, 20, ac_size] Q-features tensor:
            self.pc_q = duelling_pc_network(pc_x, self.ac_space, **kwargs)

            # Aux2: `Value function replay` network:
            # VR network is fully shared with ppo network but with `value` only output:
            # and has same off-policy batch pass with off_ppo network:
            self.vr_batch_size = self.off_batch_size
            self.vr_time_length = self.off_time_length

            self.vr_state_in = self.off_state_in
            self.vr_a_r_in = self.off_a_r_in

            self.vr_lstm_state_pl_flatten = self.off_lstm_state_pl_flatten
            self.vr_value = self.off_vf

            # Aux3: `Reward prediction` network:
            self.rp_batch_size = tf.placeholder(tf.int32, name='rp_batch_size')

            # Shared conv. output:
            rp_x = conv_2d_network(self.rp_state_in['external'], ob_space['external'], ac_space, reuse=True, **kwargs)

            # Flatten batch-wise:
            rp_x_shape_static = rp_x.get_shape().as_list()
            rp_x = tf.reshape(rp_x, [self.rp_batch_size, np.prod(rp_x_shape_static[1:]) * (self.rp_sequence_size-1)])

            # RP output:
            self.rp_logits = dense_rp_network(rp_x)

        # Batch-norm related (useless, ignore):
        try:
            if self.train_phase is not None:
                pass

        except:
            self.train_phase = tf.placeholder_with_default(
                tf.constant(False, dtype=tf.bool),
                shape=(),
                name='train_phase_flag_pl'
            )
        self.update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        # Add moving averages to save list:
        moving_var_list = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, tf.get_variable_scope().name + '.*moving.*')
        renorm_var_list = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, tf.get_variable_scope().name + '.*renorm.*')

        # What to save:
        self.var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, tf.get_variable_scope().name)
        self.var_list += moving_var_list + renorm_var_list

        # Callbacks:
        if self.aux_estimate:
            self.callback['pixel_change'] = self.get_pc_target
