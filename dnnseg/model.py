import sys
import os
import re
import math
import random
import time
import pickle
import numpy as np
import scipy.signal
import pandas as pd
import tensorflow as tf
from sklearn.metrics import homogeneity_completeness_v_measure, adjusted_mutual_info_score, fowlkes_mallows_score

from .backend import *
from .data import cache_data
from .kwargs import UNSUPERVISED_WORD_CLASSIFIER_INITIALIZATION_KWARGS, UNSUPERVISED_WORD_CLASSIFIER_MLE_INITIALIZATION_KWARGS
from .util import f_measure, pretty_print_seconds, stderr
from .plot import plot_acoustic_features, plot_label_histogram, plot_label_heatmap, plot_binary_unit_heatmap

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

tf_config = tf.ConfigProto()
tf_config.gpu_options.allow_growth = True

is_embedding_dimension = re.compile('d([0-9]+)')
regularizer = re.compile('([^_]+)(_([0-9]*\.?[0-9]*))?')


class DNNSeg(object):

    ############################################################
    # Initialization methods
    ############################################################

    _INITIALIZATION_KWARGS = UNSUPERVISED_WORD_CLASSIFIER_INITIALIZATION_KWARGS

    _doc_header = """
        Abstract base class for DNNSeg. Bayesian and MLE implementations inherit from ``DNNSeg``.
        ``DNNSeg`` is not a complete implementation and cannot be instantiated.

    """
    _doc_args = """
        :param train_data: ``AcousticDataset`` object; training data.
    \n"""
    _doc_kwargs = '\n'.join([' ' * 8 + ':param %s' % x.key + ': ' + '; '.join(
        [x.dtypes_str(), x.descr]) + ' **Default**: ``%s``.' % (
                                 x.default_value if not isinstance(x.default_value, str) else "'%s'" % x.default_value)
                             for
                             x in _INITIALIZATION_KWARGS])
    __doc__ = _doc_header + _doc_args + _doc_kwargs

    def __new__(cls, *args, **kwargs):
        if cls is DNNSeg:
            raise TypeError("UnsupervisedWordClassifier is an abstract class and may not be instantiated")
        return object.__new__(cls)

    def __init__(self, train_data, **kwargs):

        for kwarg in DNNSeg._INITIALIZATION_KWARGS:
            setattr(self, kwarg.key, kwargs.pop(kwarg.key, kwarg.default_value))
        self.speaker_list = train_data.segments().speaker.unique()

        if self.data_type.lower() == 'acoustic' and self.filter_type.lower() == 'cochleagram':
            from .cochleagram import invert_cochleagrams
            self.spectrogram_inverter = invert_cochleagrams
        else:
            self.spectrogram_inverter = None

        if self.speaker_revnet_n_layers:
            assert self.speaker_emb_dim, "DNNSeg's RevNet setting is used for speaker adaptation. Supply a non-zero value for speaker_emb_dim."
            if self.order:
                assert self.predict_deltas, 'RevNets require inputs and outputs with matched dimensionality. Use either order=0 or predict_deltas=True.'

        self._initialize_session()

    def _initialize_session(self):
        self.g = tf.Graph()
        self.sess = tf.Session(graph=self.g, config=tf_config)

    def _initialize_metadata(self):
        assert not (self.streaming and (self.task.lower() == 'classifier')), 'Streaming mode is not supported for the classifier task.'

        self.MASKED_NEIGHBORS_OLD = False

        self.FLOAT_TF = getattr(tf, self.float_type)
        self.FLOAT_NP = getattr(np, self.float_type)
        self.INT_TF = getattr(tf, self.int_type)
        self.INT_NP = getattr(np, self.int_type)
        self.UINT_TF = getattr(np, 'u' + self.int_type)
        self.UINT_NP = getattr(tf, 'u' + self.int_type)
        self.use_dtw = self.dtw_gamma is not None
        self.regularizer_map = {}
        
        if self.n_units_input_projection:
            self.project_inputs = True
            if isinstance(self.n_units_input_projection, str):
                self.units_input_projection = [int(x) for x in self.n_units_input_projection.split()]
            elif isinstance(self.n_units_input_projection, int):
                if self.n_layers_input_projection is None:
                    self.units_input_projection = [self.n_units_input_projection]
                else:
                    self.units_input_projection = [self.n_units_input_projection] * self.n_layers_input_projection
            else:
                self.units_input_projection = self.n_units_input_projection
    
            if self.n_layers_input_projection is None:
                self.layers_input_projection = len(self.units_input_projection)
            else:
                self.layers_input_projection = self.n_layers_input_projection
            if len(self.units_input_projection) == 1:
                self.units_input_projection = [self.units_input_projection[0]] * self.layers_input_projection
    
            assert len(self.units_input_projection) == self.layers_input_projection, 'Misalignment in number of layers between n_layers_input_projection and n_units_input_projection.'
        else:
            self.project_inputs = False
            self.layers_input_projection = None
            self.units_input_projection = None

        assert not self.n_units_encoder is None, 'You must provide a value for **n_units_encoder** when initializing a DNNSeg model.'
        if isinstance(self.n_units_encoder, str):
            self.units_encoder = [int(x) for x in self.n_units_encoder.split()]
        elif isinstance(self.n_units_encoder, int):
            if self.n_layers_encoder is None:
                self.units_encoder = [self.n_units_encoder]
            else:
                self.units_encoder = [self.n_units_encoder] * self.n_layers_encoder
        else:
            self.units_encoder = self.n_units_encoder

        if self.n_layers_encoder is None:
            self.layers_encoder = len(self.units_encoder)
        else:
            self.layers_encoder = self.n_layers_encoder
        if len(self.units_encoder) == 1:
            self.units_encoder = [self.units_encoder[0]] * self.layers_encoder

        assert len(self.units_encoder) == self.layers_encoder, 'Misalignment in number of layers between n_layers_encoder and n_units_encoder.'

        if isinstance(self.n_features_encoder, str):
            features_encoder = []
            for x in self.n_features_encoder.split():
                if x.lower() == 'none':
                    features_encoder.append(None)
                else:
                    features_encoder.append(int(x))
            self.features_encoder = features_encoder
        else:
            self.features_encoder = [self.n_features_encoder] * self.layers_encoder

        assert len(self.features_encoder) == self.layers_encoder, 'Misalignment in number of layers between n_features_encoder and n_units_encoder.'

        if isinstance(self.n_embdims_encoder, str):
            embdims_encoder = []
            for x in self.n_embdims_encoder.split():
                if x.lower() == 'none':
                    embdims_encoder.append(None)
                else:
                    embdims_encoder.append(int(x))
            self.embdims_encoder = embdims_encoder
        else:
            self.embdims_encoder = [self.n_embdims_encoder] * self.layers_encoder

        assert len(self.embdims_encoder) == self.layers_encoder, 'Misalignment in number of layers between n_embdims_encoder and n_units_encoder.'

        if self.decoder_concatenate_hidden_states:
            self.encoding_n_dims = sum(self.units_encoder) + self.units_encoder[-1]
        else:
            self.encoding_n_dims = self.units_encoder[-1]

        if isinstance(self.n_units_decoder, str):
            self.units_decoder = [int(x) for x in self.n_units_decoder.split()]
        elif isinstance(self.n_units_decoder, int):
            if self.n_layers_decoder is None:
                self.units_decoder = [self.n_units_decoder]
            else:
                self.units_decoder = [self.n_units_decoder] * self.n_layers_decoder
        else:
            self.units_decoder = self.n_units_decoder

        if self.units_decoder is None:
            self.layers_decoder = None
        else:
            if self.n_layers_decoder is None:
                self.layers_decoder = len(self.units_decoder)
            else:
                self.layers_decoder = self.n_layers_decoder
            if len(self.units_decoder) == 1:
                self.units_decoder = [self.units_decoder[0]] * self.layers_decoder

            assert len(self.units_decoder) == self.n_layers_decoder, 'Misalignment in number of layers between n_layers_decoder and n_units_decoder.'
            
        if self.n_units_decoder_input_projection:
            self.project_decoder_inputs = True
            if isinstance(self.n_units_decoder_input_projection, str):
                self.units_decoder_input_projection = [int(x) for x in self.n_units_decoder_input_projection.split()]
            elif isinstance(self.n_units_decoder_input_projection, int):
                if self.n_layers_decoder_input_projection is None:
                    self.units_decoder_input_projection = [self.n_units_decoder_input_projection]
                else:
                    self.units_decoder_input_projection = [self.n_units_decoder_input_projection] * self.n_layers_decoder_input_projection
            else:
                self.units_decoder_input_projection = self.n_units_decoder_input_projection
    
            if self.n_layers_decoder_input_projection is None:
                self.layers_decoder_input_projection = len(self.units_decoder_input_projection)
            else:
                self.layers_decoder_input_projection = self.n_layers_decoder_input_projection
            if len(self.units_decoder_input_projection) == 1:
                self.units_decoder_input_projection = [self.units_decoder_input_projection[0]] * self.layers_decoder_input_projection
    
            assert len(self.units_decoder_input_projection) == self.layers_decoder_input_projection, 'Misalignment in number of layers between n_layers_decoder_input_projection and n_units_decoder_input_projection.'
        else:
            self.project_decoder_inputs = False
            self.layers_decoder_input_projection = None
            self.units_decoder_input_projection = None

        if self.n_units_correspondence_decoder:
            self.use_correspondence_decoder = True
            if isinstance(self.n_units_correspondence_decoder, str):
                self.units_correspondence_decoder = [int(x) for x in self.n_units_correspondence_decoder.split()]
            elif isinstance(self.n_units_correspondence_decoder, int):
                if self.n_layers_correspondence_decoder is None:
                    self.units_correspondence_decoder = [self.n_units_correspondence_decoder]
                else:
                    self.units_correspondence_decoder = [self.n_units_correspondence_decoder] * self.n_layers_correspondence_decoder
            else:
                self.units_correspondence_decoder = self.n_units_correspondence_decoder

            if self.n_layers_correspondence_decoder is None:
                self.layers_correspondence_decoder = len(self.units_correspondence_decoder)
            else:
                self.layers_correspondence_decoder = self.n_layers_correspondence_decoder
            if len(self.units_correspondence_decoder) == 1:
                self.units_correspondence_decoder = [self.units_correspondence_decoder[0]] * self.layers_correspondence_decoder

            assert len(self.units_correspondence_decoder) == self.n_layers_correspondence_decoder, 'Misalignment in number of layers between n_layers_correspondence_decoder and n_units_correspondence_decoder.'
        else:
            self.use_correspondence_decoder = False

        if self.segment_encoding_correspondence_regularizer_scale and \
                self.encoder_type.lower() in ['cnn_hmlstm' ,'hmlstm'] and \
                self.layers_encoder == self.n_layers_decoder and \
                self.units_encoder == self.units_decoder[::-1]:
            self.regularize_correspondences = True
        else:
            self.regularize_correspondences = False

        if self.regularize_correspondences and self.resample_inputs == self.resample_targets_bwd:
            self.matched_correspondences = True
        else:
            self.matched_correspondences = False

        if self.pad_seqs:
            if self.mask_padding and ('hmlstm' in self.encoder_type.lower() or 'rnn' in self.encoder_type.lower()):
                self.input_padding = 'post'
            else:
                self.input_padding = 'pre'
            self.target_padding = 'post'
        else:
            self.input_padding = None
            self.target_padding = None

        if self.max_len:
            max_len = self.max_len
        else:
            max_len = np.inf

        if self.resample_inputs:
            resample_inputs = self.resample_inputs
        else:
            resample_inputs = np.inf

        if self.resample_targets_bwd:
            resample_targets_bwd = self.resample_targets_bwd
        else:
            resample_targets_bwd = np.inf

        if self.resample_targets_fwd:
            resample_targets_fwd = self.resample_targets_fwd
        else:
            resample_targets_fwd = np.inf

        if self.resample_inputs:
            self.n_timestamps_input = min(max_len, resample_inputs)
        else:
            self.n_timestamps_input = max_len

        if self.resample_targets_bwd:
            if self.streaming:
                self.n_timesteps_output_bwd = min(self.window_len_bwd, resample_targets_bwd)
            else:
                self.n_timesteps_output_bwd = min(self.max_len, resample_targets_bwd)
        else:
            if self.streaming:
                self.n_timesteps_output_bwd = min(self.window_len_bwd, resample_targets_bwd)
            else:
                self.n_timesteps_output_bwd = min(self.max_len, resample_targets_bwd)

        if self.streaming and self.predict_forward:
            if self.resample_targets_fwd:
                    self.n_timesteps_output_fwd = min(self.window_len_fwd, resample_targets_fwd)
            else:
                self.n_timesteps_output_fwd = self.window_len_fwd

        if self.lm_loss_scale:
            assert self.lm_order_bwd + self.lm_order_fwd > 0, 'When LM loss is turned on, order of language model must be > 0.'
            self.use_lm_loss = False
        else:
            self.use_lm_loss = False
        if isinstance(self.lm_loss_scale, str):
            if self.lm_loss_scale.startswith('exp'):
                base = int(self.lm_loss_scale[3])
                self.lm_loss_scale = [1 / (base**l) for l in range(self.layers_encoder)]
            else:
                self.lm_loss_scale = [float(x) for x in self.lm_loss_scale.split()]
                if len(self.lm_loss_scale) == 1:
                    self.lm_loss_scale = [self.lm_loss_scale[0]] * self.layers_encoder
        elif isinstance(self.lm_loss_scale, float):
            self.lm_loss_scale = [self.lm_loss_scale] * self.layers_encoder
        else:
            self.lm_loss_scale = self.lm_loss_scale
        assert len(self.lm_loss_scale) == self.layers_encoder, 'Misalignment in number of layers between lm_loss_scale and n_units_encoder.'

        if isinstance(self.lm_gradient_scale, str):
            self.lm_gradient_scale = [float(x) for x in self.lm_gradient_scale.split()]
            if len(self.lm_gradient_scale) == 1:
                self.lm_gradient_scale = [self.lm_gradient_scale[0]] * self.layers_encoder
        elif isinstance(self.lm_gradient_scale, float) or self.lm_gradient_scale is None:
            self.lm_gradient_scale = [self.lm_gradient_scale] * self.layers_encoder
        else:
            self.lm_gradient_scale = self.lm_gradient_scale
        assert len(self.lm_gradient_scale) == self.layers_encoder, 'Misalignment in number of layers between lm_gradient_scale and n_units_encoder.'
        
        if isinstance(self.lm_target_gradient_scale, str):
            self.lm_target_gradient_scale = [float(x) for x in self.lm_target_gradient_scale.split()]
            if len(self.lm_target_gradient_scale) == 1:
                self.lm_target_gradient_scale = [self.lm_target_gradient_scale[0]] * self.layers_encoder
        elif isinstance(self.lm_target_gradient_scale, float) or self.lm_target_gradient_scale is None:
            self.lm_target_gradient_scale = [self.lm_target_gradient_scale] * self.layers_encoder
        else:
            self.lm_target_gradient_scale = self.lm_target_gradient_scale
        assert len(self.lm_target_gradient_scale) == self.layers_encoder, 'Misalignment in number of layers between lm_target_gradient_scale and n_units_encoder.'

        if self.correspondence_loss_scale:
            self.use_correspondence_loss = True
        else:
            self.use_correspondence_loss = False
        if isinstance(self.correspondence_loss_scale, str):
            self.correspondence_loss_scale = [float(x) for x in self.correspondence_loss_scale.split()]
            if len(self.correspondence_loss_scale) == 1:
                self.correspondence_loss_scale = [self.correspondence_loss_scale[0]] * (self.layers_encoder - 1)
        elif isinstance(self.correspondence_loss_scale, float) or self.correspondence_loss_scale is None:
            self.correspondence_loss_scale = [self.correspondence_loss_scale] * (self.layers_encoder - 1)
        else:
            self.correspondence_loss_scale = self.correspondence_loss_scale
        assert len(self.correspondence_loss_scale) == self.layers_encoder - 1, 'Misalignment in number of layers between correspondence_loss_scale and n_units_encoder.'

        if isinstance(self.correspondence_gradient_scale, str):
            self.correspondence_gradient_scale = [float(x) for x in self.correspondence_gradient_scale.split()]
            if len(self.correspondence_gradient_scale) == 1:
                self.correspondence_gradient_scale = [self.correspondence_gradient_scale[0]] * self.layers_encoder
        elif isinstance(self.correspondence_gradient_scale, float) or self.correspondence_gradient_scale is None:
            self.correspondence_gradient_scale = [self.correspondence_gradient_scale] * self.layers_encoder
        else:
            self.correspondence_gradient_scale = self.correspondence_gradient_scale
        assert len(self.correspondence_gradient_scale) == self.layers_encoder, 'Misalignment in number of layers between correspondence_gradient_scale and n_units_encoder.'

        ppx = self.plot_position_index.split()
        if len(ppx) == 1:
            ppx *= 2
        ppx_bwd, ppx_fwd = ppx
        if ppx_bwd.lower() != 'mid':
            ppx_bwd = int(ppx_bwd)
        if ppx_fwd.lower() != 'mid':
            ppx_fwd = int(ppx_fwd)

        self.ppx_bwd = ppx_bwd
        self.ppx_fwd = ppx_fwd

        if self.streaming:
            self.train_data_name = 'streaming'
        else:
            self.train_data_name = 'utt'

        val_data_name = 'files'
        if self.task.lower() == 'classifier':
            val_data_name = 'utt'
        self.val_data_name = val_data_name

        plot_data_name = 'utt'
        if self.streaming:
            plot_data_name = 'streaming'
        self.plot_data_name = plot_data_name

        with self.sess.as_default():
            with self.sess.graph.as_default():
                if self.entropy_regularizer_scale:
                    self.entropy_regularizer = binary_entropy_regularizer(
                        scale=self.entropy_regularizer_scale,
                        from_logits=False,
                        session=self.sess
                    )
                else:
                    self.entropy_regularizer = None

                def get_extremeness_regularizer(shape=0.01, scale=1):
                    def extremeness_regularizer(x):
                        out = tf.contrib.distributions.Beta(
                            shape,
                            shape
                        ).prob((1 - self.epsilon) * x) * scale

                        return out

                    return extremeness_regularizer

                if self.boundary_rate_extremeness_regularizer_scale:
                    assert self.boundary_rate_extremeness_regularizer_shape >= 0 and self.boundary_rate_extremeness_regularizer_shape <= 1, 'boundary_rate_extremeness_regularizer_shape must be in [0,1], got %s.' % self.boundary_rate_extremeness_regularizer_shape

                    self.boundary_rate_extremeness_regularizer = get_extremeness_regularizer(
                        shape=self.boundary_rate_extremeness_regularizer_shape,
                        scale=self.boundary_rate_extremeness_regularizer_scale
                    )
                else:
                    self.boundary_rate_extremeness_regularizer = None
                    
                if self.boundary_prob_extremeness_regularizer_scale:
                    assert self.boundary_prob_extremeness_regularizer_shape >= 0 and self.boundary_prob_extremeness_regularizer_shape <= 1, 'boundary_prob_extremeness_regularizer_shape must be in [0,1], got %s.' % self.boundary_prob_extremeness_regularizer_shape
                    self.boundary_prob_extremeness_regularizer = get_extremeness_regularizer(
                        shape=self.boundary_prob_extremeness_regularizer_shape,
                        scale=self.boundary_prob_extremeness_regularizer_scale
                    )
                else:
                    self.boundary_prob_extremeness_regularizer = None

                if self.boundary_prob_regularizer_scale:
                    self.boundary_prob_regularizer = lambda bit_probs: tf.reduce_mean(bit_probs) * self.boundary_prob_regularizer_scale
                else:
                    self.boundary_prob_regularizer = None

                if self.boundary_regularizer_scale:
                    self.boundary_regularizer = lambda bit_probs: tf.reduce_mean(bit_probs) * self.boundary_regularizer_scale
                else:
                    self.boundary_regularizer = None

                if self.encoder_state_regularization:
                    self.encoder_state_regularizer = get_regularizer(self.encoder_state_regularization, session=self.sess)
                else:
                    self.encoder_state_regularizer = None
                    
                if self.encoder_feature_regularization:
                    self.encoder_feature_regularizer = get_regularizer(self.encoder_feature_regularization, session=self.sess)
                else:
                    self.encoder_feature_regularizer = None

                if self.encoder_bitwise_feature_regularization:
                    self.encoder_bitwise_feature_regularizer = get_regularizer(self.encoder_bitwise_feature_regularization, session=self.sess)
                else:
                    self.encoder_bitwise_feature_regularizer = None

                if self.encoder_cell_proposal_regularization:
                    self.encoder_cell_proposal_regularizer = get_regularizer(self.encoder_cell_proposal_regularization, session=self.sess)
                else:
                    self.encoder_cell_proposal_regularizer = None

                if self.segment_encoding_correspondence_regularizer_scale:
                    # self.segment_encoding_correspondence_regularizer = mse_regularizer(scale=self.segment_encoding_correspondence_regularizer_scale, session=self.sess)
                    # self.segment_encoding_correspondence_regularizer = cross_entropy_regularizer(scale=self.segment_encoding_correspondence_regularizer_scale, session=self.sess)
                    self.segment_encoding_correspondence_regularizer = tf.contrib.layers.l1_regularizer(scale=self.segment_encoding_correspondence_regularizer_scale)
                else:
                    self.segment_encoding_correspondence_regularizer = None

        self.label_map = None
        if self.label_map_file:
            if os.path.exists(self.label_map_file):
                label_map = pd.read_csv(self.label_map_file)
                label_map = dict(zip(label_map.source, label_map.target))
                self.label_map = label_map
            else:
                stderr('Label map file %s does not exist. Label mapping will not be used.' %self.label_map_file)
                
        self.feature_map = None
        if self.feature_map_file:
            if os.path.exists(self.feature_map_file):
                self.feature_map = pd.read_csv(self.feature_map_file)
            else:
                stderr('Feature map file %s does not exist. Feature mapping will not be used.' %self.feature_map_file)

        self.predict_mode = False

    def _pack_metadata(self):
        if hasattr(self, 'n_train'):
            n_train = self.n_train
        else:
            n_train = None
        md = {
            'speaker_list': self.speaker_list,
            'n_train': n_train
        }
        for kwarg in DNNSeg._INITIALIZATION_KWARGS:
            md[kwarg.key] = getattr(self, kwarg.key)
        return md

    def _unpack_metadata(self, md):
        self.speaker_list = md.pop('speaker_list', [])
        self.n_train = md.pop('n_train', None)
        for kwarg in DNNSeg._INITIALIZATION_KWARGS:
            setattr(self, kwarg.key, md.pop(kwarg.key, kwarg.default_value))

    def __getstate__(self):
        return self._pack_metadata()

    def __setstate__(self, state):
        self._unpack_metadata(state)
        self._initialize_session()
        self._initialize_metadata()






    ############################################################
    # Private model construction methods
    ############################################################

    def build(self, n_train=None, outdir=None, restore=True, verbose=True):
        if outdir is None:
            if not hasattr(self, 'outdir'):
                self.outdir = './dnnseg_model/'
        else:
            self.outdir = outdir

        if n_train is not None:
            self.n_train = n_train

        if not hasattr(self, 'n_train') or self.n_train is None:
            raise ValueError('Build parameter "n_train" must be provided the first time build() is called.')

        self._initialize_inputs()
        if self.task != 'streaming_autoencoder':
            self.encoder = self._initialize_encoder(self.inputs)
            self.encoding = self._initialize_classifier(self.encoder)
            self.decoder_in, self.extra_dims = self._augment_encoding(self.encoding, encoder=self.encoder)

            if self.task == 'segmenter':
                if not self.streaming or self.predict_backward:
                    if self.mask_padding:
                        mask = self.y_bwd_mask
                    else:
                        mask = None
                    with tf.variable_scope('decoder_bwd'):
                        self.decoder_bwd, self.pe_bwd = self._initialize_decoder(
                            self.decoder_in,
                            self.n_timesteps_output_bwd,
                            mask=mask,
                            decoder_hidden_state_expansion_type=self.decoder_hidden_state_expansion_type,
                            decoder_positional_encoding_type=self.decoder_positional_encoding_type,
                            decoder_positional_encoding_as_mask=self.decoder_positional_encoding_as_mask,
                            decoder_positional_encoding_units=self.decoder_positional_encoding_units,
                            decoder_positional_encoding_transform=self.decoder_positional_encoding_transform,
                            decoder_positional_encoding_activation=self.decoder_positional_encoding_activation,
                            name='decoder_bwd'
                        )

                if self.streaming and self.predict_forward:
                    if self.mask_padding:
                        mask = self.y_fwd_mask
                    else:
                        mask = None
                    with tf.variable_scope('decoder_fwd'):
                        self.decoder_fwd, self.pe_fwd = self._initialize_decoder(
                            self.decoder_in,
                            self.n_timesteps_output_fwd,
                            mask=mask,
                            decoder_hidden_state_expansion_type=self.decoder_hidden_state_expansion_type,
                            decoder_positional_encoding_type=self.decoder_positional_encoding_type,
                            decoder_positional_encoding_as_mask=self.decoder_positional_encoding_as_mask,
                            decoder_positional_encoding_units=self.decoder_positional_encoding_units,
                            decoder_positional_encoding_transform=self.decoder_positional_encoding_transform,
                            decoder_positional_encoding_activation=self.decoder_positional_encoding_activation,
                            name='decoder_fwd'
                        )
            else:
                if self.mask_padding:
                    mask = self.y_bwd_mask
                else:
                    mask = None
                with tf.variable_scope('decoder_bwd'):
                    self.decoder_bwd, self.pe_bwd = self._initialize_decoder(
                        self.decoder_in,
                        self.n_timesteps_output_bwd,
                        mask=mask,
                        decoder_hidden_state_expansion_type=self.decoder_hidden_state_expansion_type,
                        decoder_positional_encoding_type=self.decoder_positional_encoding_type,
                        decoder_positional_encoding_as_mask=self.decoder_positional_encoding_as_mask,
                        decoder_positional_encoding_units=self.decoder_positional_encoding_units,
                        decoder_positional_encoding_transform=self.decoder_positional_encoding_transform,
                        decoder_positional_encoding_activation=self.decoder_positional_encoding_activation,
                        name='decoder_bwd'
                    )

            self._initialize_output_model()
        self._initialize_objective(n_train)
        self._initialize_ema()
        self._initialize_saver()
        self._initialize_logging()

        with self.sess.as_default():
            with self.sess.graph.as_default():
                self.report_uninitialized = tf.report_uninitialized_variables(
                    var_list=None
                )
        self.load(restore=restore)

        self.sess.graph.finalize()

    def _initialize_inputs(self):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                self.training = tf.placeholder_with_default(tf.constant(True, dtype=tf.bool), shape=[], name='training')

                if self.data_type.lower() == 'acoustic':
                    self.input_dim = self.n_coef * (self.order + 1)
                else:
                    self.input_dim = self.n_coef

                if self.predict_deltas:
                    self.frame_dim = self.n_coef * (self.order + 1)
                else:
                    self.frame_dim = self.n_coef

                self.speaker = tf.placeholder(tf.string, shape=[None], name='speaker')
                self.speaker_table, self.speaker_embedding_matrix = initialize_embeddings(
                    self.speaker_list,
                    self.speaker_emb_dim if self.speaker_emb_dim else None,
                    name='speaker_embedding',
                    session=self.sess
                )
                self.speaker_one_hot = tf.one_hot(
                    self.speaker_table.lookup(self.speaker),
                    len(self.speaker_list) + 1,
                    dtype=self.FLOAT_TF
                )
                if self.speaker_emb_dim:
                    if self.optim_name == 'Nadam':  # Nadam can't handle sparse embedding lookup, so do it with matmul
                        self.speaker_embeddings = tf.matmul(
                            self.speaker_one_hot,
                            self.speaker_embedding_matrix
                        )
                    else:
                        self.speaker_embeddings = tf.nn.embedding_lookup(
                            self.speaker_embedding_matrix,
                            self.speaker_table.lookup(self.speaker)
                        )

                self.X = tf.placeholder(self.FLOAT_TF, shape=(None, self.n_timesteps_input, self.input_dim), name='X')
                self.X_mask = tf.placeholder_with_default(
                    tf.ones(tf.shape(self.X)[:-1], dtype=self.FLOAT_TF),
                    shape=(None, self.n_timesteps_input),
                    name='X_mask'
                )
                self.fixed_boundaries_placeholder = tf.placeholder_with_default(
                    tf.zeros_like(self.X_mask, dtype=self.FLOAT_TF),
                    shape=(None, self.n_timesteps_input),
                    name='fixed_boundaries'
                )

                self.X_feat_mean = tf.reduce_sum(self.X, axis=-2) / tf.reduce_sum(self.X_mask, axis=-1, keepdims=True)
                self.X_time_mean = tf.reduce_mean(self.X, axis=-1)

                X = self.X

                if self.input_batch_normalization_decay:
                    X = tf.contrib.layers.batch_norm(
                        X,
                        decay=self.input_batch_normalization_decay,
                        center=True,
                        scale=True,
                        zero_debias_moving_mean=True,
                        is_training=self.training,
                        updates_collections=None,
                        scope='input'
                    )

                if self.speaker_emb_dim and self.append_speaker_emb_to_inputs and not self.speaker_revnet_n_layers:
                    tiled_embeddings = tf.tile(self.speaker_embeddings[:, None, :], [1, tf.shape(self.X)[1], 1])
                    self.inputs = tf.concat([X, tiled_embeddings], axis=-1)
                else:
                    self.inputs = X

                if self.project_inputs:
                    projection_in = self.inputs
                    projection_lambdas = []
                    depth = self.layers_input_projection
                    dense_kernel_initializer = 'identity_initializer'
                    resnet_kernel_initializer = 'glorot_uniform_initializer'

                    for d in range(depth):
                        if d == depth - 1:
                            activation = self.input_projection_activation
                        else:
                            activation = self.input_projection_activation_inner
                        name_cur = 'input_projection_d%d' % d

                        if self.encoder_resnet_n_layers_inner and self.encoder_resnet_n_layers_inner > 1:
                            kernel_layer = DenseResidualLayer(
                                training=self.training,
                                units=self.units_input_projection[d],
                                kernel_initializer=resnet_kernel_initializer,
                                layers_inner=self.decoder_resnet_n_layers_inner,
                                activation_inner=activation,
                                activation=activation,
                                sample_at_train=self.sample_at_train,
                                sample_at_eval=self.sample_at_eval,
                                batch_normalization_decay=self.decoder_batch_normalization_decay,
                                project_inputs=False,
                                session=self.sess,
                                name=name_cur
                            )
                        else:
                            kernel_layer = DenseLayer(
                                training=self.training,
                                units=self.units_input_projection[d],
                                kernel_initializer=dense_kernel_initializer,
                                activation=activation,
                                sample_at_train=self.sample_at_train,
                                sample_at_eval=self.sample_at_eval,
                                batch_normalization_decay=self.decoder_batch_normalization_decay,
                                session=self.sess,
                                reuse=tf.AUTO_REUSE,
                                name=name_cur
                            )

                        projection_lambdas.append(make_lambda(kernel_layer, session=self.sess))

                    projection_fn = compose_lambdas(projection_lambdas)

                    self.inputs = projection_fn(projection_in)

                acoustic_feats = tf.boolean_mask(X, self.X_mask)
                mean, std = tf.nn.moments(acoustic_feats, axes=[0, 1])
                X_max = tf.reduce_max(X)
                X_min = tf.reduce_min(X)

                # self.inputs = tf.Print(self.inputs, [mean, std, X_max, X_min])

                if self.speaker_revnet_n_layers:
                    self.speaker_revnet = RevNet(
                        training=self.training,
                        layers=self.speaker_revnet_n_layers,
                        layers_inner=self.speaker_revnet_n_layers_inner,
                        activation=self.speaker_revnet_activation,
                        batch_normalization_decay=self.speaker_revnet_batch_normalization_decay,
                        session=self.sess,
                        name='SpeakerRevNet'
                    )

                if not self.streaming or self.predict_backward:
                    self.y_bwd = tf.placeholder(self.FLOAT_TF, shape=(None, self.n_timesteps_output_bwd, self.frame_dim), name='y_bwd')
                    self.y_bwd_mask = tf.placeholder_with_default(
                        tf.ones(tf.shape(self.y_bwd)[:-1], dtype=self.FLOAT_TF),
                        shape=(None, self.n_timesteps_output_bwd),
                        name='y_bwd_mask'
                    )

                if self.streaming and self.predict_forward:
                    self.y_fwd = tf.placeholder(self.FLOAT_TF, shape=(None, self.n_timesteps_output_fwd, self.frame_dim), name='y_fwd')
                    self.y_fwd_mask = tf.placeholder_with_default(
                        tf.ones(tf.shape(self.y_fwd)[:-1], dtype=self.FLOAT_TF),
                        shape=(None, self.n_timesteps_output_fwd),
                        name='y_fwd_mask'
                    )

                if self.oracle_boundaries:
                    self.oracle_boundaries_placeholder = tf.placeholder(
                        self.FLOAT_TF,
                        shape=(None, self.n_timesteps_input, self.layers_encoder - 1),
                        name='oracle_boundaries'
                    )

                self.global_step = tf.Variable(
                    0,
                    trainable=False,
                    dtype=self.INT_TF,
                    name='global_step'
                )
                self.incr_global_step = tf.assign(self.global_step, self.global_step + 1)
                self.global_batch_step = tf.Variable(
                    0,
                    trainable=False,
                    dtype=self.INT_TF,
                    name='global_batch_step'
                )
                self.incr_global_batch_step = tf.assign(self.global_batch_step, self.global_batch_step + 1)
                if self.streaming:
                    self.step = self.global_batch_step
                else:
                    self.step = self.global_step

                if self.streaming and self.curriculum_type:
                    if self.curriculum_type.lower() == 'hard':
                        self.curriculum_t = tf.cast(self.curriculum_init + self.step / self.curriculum_steps, self.INT_TF)
                    elif self.curriculum_type.lower() == 'exp':
                        self.curriculum_t = self.curriculum_init + tf.cast(self.step, dtype=self.FLOAT_TF) / self.curriculum_steps
                    elif self.curriculum_type.lower() == 'sigmoid':
                        self.curriculum_t = tf.cast(self.step, dtype=self.FLOAT_TF) / self.curriculum_steps
                    else:
                        raise ValueError('Unsupported value "%s" for curriculum type.' % self.curriculum_type)
                else:
                    self.curriculum_t = None

                self.loss_summary = tf.placeholder(tf.float32, name='loss_summary_placeholder')
                if self.predict_backward:
                    self.loss_reconstruction_summary = tf.placeholder(tf.float32, name='loss_reconstruction_summary_placeholder')
                if self.predict_forward:
                    self.loss_prediction_summary = tf.placeholder(tf.float32, name='loss_prediction_summary_placeholder')
                if self.lm_loss_scale:
                    self.encoder_lm_loss_summary = []
                    for l in range(self.layers_encoder):
                        self.encoder_lm_loss_summary.append(
                            tf.placeholder(tf.float32, name='encoder_lm_loss_%d_summary_placeholder' % (l+1))
                        )
                if self.speaker_adversarial_gradient_scale:
                    self.encoder_speaker_adversarial_loss_summary = []
                    for l in range(self.layers_encoder):
                        self.encoder_speaker_adversarial_loss_summary.append(
                            tf.placeholder(tf.float32, name='encoder_speaker_adversarial_loss_%d_summary_placeholder' % (l + 1))
                        )
                if self.passthru_adversarial_gradient_scale and self.n_passthru_neurons:
                    self.encoder_passthru_adversarial_loss_summary = []
                    for l in range(self.layers_encoder):
                        self.encoder_passthru_adversarial_loss_summary.append(
                            tf.placeholder(tf.float32, name='encoder_passthru_adversarial_loss_%d_summary_placeholder' % (l + 1))
                        )
                self.reg_summary = tf.placeholder(tf.float32, name='reg_summary_placeholder')

                self.classification_scores = []
                if self.task.lower() == 'classifier':
                    n_layers = 1
                else:
                    n_layers = self.layers_encoder - 1
                for l in range(n_layers):
                    self.classification_scores.append(
                        {
                            'phn': {
                                'goldseg': {
                                    'system': {},
                                    'random': {}
                                },
                                'predseg': {
                                    'system': {},
                                    'random': {}
                                }
                            },
                            'wrd': {
                                'goldseg': {
                                    'system': {},
                                    'random': {}
                                },
                                'predseg': {
                                    'system': {},
                                    'random': {}
                                }
                            }
                        }
                    )
                    for s in ['phn', 'wrd']:
                        for g in ['goldseg', 'predseg']:
                            for t in ['system', 'random']:
                                self.classification_scores[-1][s][g][t] = {
                                    'homogeneity': tf.placeholder(tf.float32, name='homogeneity_l%d_%s_%s_%s' % (l+1, s, g, t)),
                                    'completeness': tf.placeholder(tf.float32, name='completeness_l%d_%s_%s_%s' % (l+1, s, g, t)),
                                    'v_measure': tf.placeholder(tf.float32, name='v_measure_l%d_%s_%s_%s' % (l+1, s, g, t)),
                                    # 'ami': tf.placeholder(tf.float32, name='ami_l%d_%s_%s_%s' % (l+1, s, g, t)),
                                    'fmi': tf.placeholder(tf.float32, name='fmi_l%d_%s_%s_%s' % (l+1, s, g, t))
                                }

                if 'hmlstm' in self.encoder_type.lower():
                    self.segmentation_scores = []
                    for l in range(self.layers_encoder - 1):
                        self.segmentation_scores.append({'phn': {}, 'wrd': {}})
                        for s in ['phn', 'wrd']:
                            self.segmentation_scores[-1][s] = {
                                'b_p': tf.placeholder(dtype=self.FLOAT_TF, shape=[], name='b_p_%d_%s_placeholder' %(l+1, s)),
                                'b_r': tf.placeholder(dtype=self.FLOAT_TF, shape=[], name='b_r_%d_%s_placeholder' %(l+1, s)),
                                'b_f': tf.placeholder(dtype=self.FLOAT_TF, shape=[], name='b_f_%d_%s_placeholder' %(l+1, s)),
                                'w_p': tf.placeholder(dtype=self.FLOAT_TF, shape=[], name='w_p_%d_%s_placeholder' %(l+1, s)),
                                'w_r': tf.placeholder(dtype=self.FLOAT_TF, shape=[], name='w_r_%d_%s_placeholder' %(l+1, s)),
                                'w_f': tf.placeholder(dtype=self.FLOAT_TF, shape=[], name='w_f_%d_%s_placeholder' %(l+1, s)),
                                'l_p': tf.placeholder(dtype=self.FLOAT_TF, shape=[], name='l_p_%d_%s_placeholder' % (l+1, s)),
                                'l_r': tf.placeholder(dtype=self.FLOAT_TF, shape=[], name='l_r_%d_%s_placeholder' % (l+1, s)),
                                'l_f': tf.placeholder(dtype=self.FLOAT_TF, shape=[], name='l_f_%d_%s_placeholder' % (l+1, s))
                            }

                if self.task == 'streaming_autoencoder':
                    self.new_series = tf.placeholder(self.FLOAT_TF)

                if self.correspondence_loss_scale:
                    self.correspondence_loss_summary = []
                    if self.n_correspondence:
                        self.correspondence_hidden_state_placeholders = []
                        self.correspondence_feature_placeholders = []
                        if self.speaker_emb_dim:
                            self.correspondence_speaker_placeholders = []
                            self.correspondence_speaker_embeddings = []

                    for l in range(self.layers_encoder - 1):
                        self.correspondence_loss_summary.append(
                            tf.placeholder(tf.float32, name='correspondence_loss_%d_summary_placeholder' % (l + 1))
                        )
                        if self.n_correspondence:
                            correspondence_embedding = tf.placeholder_with_default(
                                tf.zeros(shape=[self.n_correspondence, self.units_encoder[l]], dtype=self.FLOAT_TF),
                                shape=[self.n_correspondence, self.units_encoder[l]],
                                name='embedding_correspondence_l%d' % (l+1)
                            )
                            correspondence_features = tf.placeholder_with_default(
                                tf.zeros(shape=[self.n_correspondence, self.resample_correspondence, self.frame_dim], dtype=self.FLOAT_TF),
                                shape=[self.n_correspondence, self.resample_correspondence, self.frame_dim],
                                name='X_correspondence_l%d' % (l+1)
                            )

                            self.correspondence_hidden_state_placeholders.append(correspondence_embedding)
                            self.correspondence_feature_placeholders.append(correspondence_features)

                            if self.speaker_emb_dim:
                                correspondence_speaker = tf.placeholder_with_default(
                                    tf.tile(tf.constant([''], tf.string), [self.n_correspondence]),
                                    shape=[self.n_correspondence],
                                    name='correspondence_speaker_l%d' % (l+1)
                                )
                                self.correspondence_speaker_placeholders.append(correspondence_speaker)

                                if self.optim_name == 'Nadam':  # Nadam can't handle sparse embedding lookup, so do it with matmul
                                    correspondence_speaker_embeddings = tf.matmul(
                                        self.speaker_one_hot,
                                        self.speaker_embedding_matrix
                                    )
                                else:
                                    correspondence_speaker_embeddings = tf.nn.embedding_lookup(
                                        self.speaker_embedding_matrix,
                                        self.speaker_table.lookup(correspondence_speaker)
                                    )
                                self.correspondence_speaker_embeddings.append(correspondence_speaker_embeddings)

                self.initial_evaluation_complete = tf.Variable(
                    tf.constant(False, dtype=tf.bool),
                    trainable=False,
                    name='initial_evaluation_complete'
                )
                self.set_initial_evaluation_complete = tf.assign(self.initial_evaluation_complete, True)

    def _initialize_encoder(self, encoder_in):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if self.batch_normalize_encodings:
                    encoding_batch_normalization_decay = self.encoder_batch_normalization_decay
                else:
                    encoding_batch_normalization_decay = None

                if self.mask_padding:
                    mask = self.X_mask
                else:
                    mask = None

                encoder = encoder_in

                if self.speaker_revnet_n_layers:
                    encoder = self.speaker_revnet.forward(encoder, weights=self.speaker_embeddings)

                if self.temporal_dropout_rate is not None and not self.encoder_type.lower() in ['cnn_hmlstm', 'hmlstm']:
                    encoder = tf.layers.dropout(
                        encoder,
                        self.temporal_dropout_rate[0],
                        noise_shape=[tf.shape(encoder)[0], tf.shape(encoder)[1], 1],
                        training=self.training
                    )

                units_utt = self.units_encoder[-1]
                if self.emb_dim:
                    units_utt += self.emb_dim

                if self.encoder_type.lower() in ['cnn_hmlstm', 'hmlstm']:
                    if self.encoder_type.lower() == 'cnn_hmlstm':
                        encoder = Conv1DLayer(
                            self.encoder_conv_kernel_size,
                            training=self.training,
                            n_filters=self.frame_dim,
                            padding='same',
                            activation=self.encoder_inner_activation,
                            batch_normalization_decay=self.encoder_batch_normalization_decay,
                            session=self.sess,
                            name='hmlstm_pre_cnn'
                        )(encoder)

                    # encoder = DenseResidualLayer(
                    #     training=self.training,
                    #     units=encoder.shape[-1],
                    #     layers_inner=self.encoder_resnet_n_layers_inner,
                    #     activation=tf.nn.elu,
                    #     activation_inner=tf.nn.elu,
                    #     batch_normalization_decay=self.encoder_batch_normalization_decay,
                    #     session=self.sess
                    # )(encoder)
                    #
                    # encoder = RNNLayer(
                    #     training=self.training,
                    #     units=int((int(encoder.shape[-1]) + self.units_encoder[0])/2),
                    #     activation=self.encoder_inner_activation,
                    #     batch_normalization_decay=self.encoder_batch_normalization_decay,
                    #     name='pre_rnn_1',
                    #     session=self.sess
                    # )(encoder)
                    #
                    # encoder = RNNLayer(
                    #     training=self.training,
                    #     units=int((int(encoder.shape[-1]) + self.units_encoder[0])/2),
                    #     activation=self.encoder_inner_activation,
                    #     batch_normalization_decay=self.encoder_batch_normalization_decay,
                    #     name='pre_rnn_2',
                    #     session=self.sess
                    # )(encoder)

                    if self.oracle_boundaries:
                        boundaries = self.oracle_boundaries_placeholder
                    elif self.encoder_force_vad_boundaries:
                        boundaries = self.fixed_boundaries_placeholder[..., None]
                        boundaries = tf.tile(boundaries, [1,1,self.layers_encoder-1])
                    else:
                        boundaries = None

                    if self.lm_loss_type.lower() == 'srn' and self.lm_loss_scale is not None:
                        return_lm_predictions = True
                        if self.speaker_adversarial_gradient_scale:
                            decoder_embedding = self.speaker_embeddings
                        else:
                            decoder_embedding = None
                    else:
                        return_lm_predictions = False
                        decoder_embedding = None

                    if self.MASKED_NEIGHBORS_OLD:
                        return_lm_predictions=True

                    self.segmenter = HMLSTMSegmenter(
                        self.units_encoder[:-1] + [units_utt],
                        self.layers_encoder,
                        num_features=self.features_encoder,
                        num_embdims=self.embdims_encoder,
                        training=self.training,
                        neurons_per_boundary=self.neurons_per_boundary,
                        boundary_neuron_agg_fn=self.boundary_neuron_agg_fn,
                        neurons_per_feature=self.neurons_per_feature,
                        feature_neuron_agg_fn=self.feature_neuron_agg_fn,
                        cumulative_boundary_prob=self.cumulative_boundary_prob,
                        cumulative_feature_prob=self.cumulative_feature_prob,
                        forget_at_boundary=self.forget_at_boundary,
                        recurrent_at_forget=self.recurrent_at_forget,
                        renormalize_preactivations=self.encoder_renormalize_preactivations,
                        kernel_depth=self.hmlstm_kernel_depth,
                        prefinal_mode=self.hmlstm_prefinal_mode,
                        resnet_n_layers=self.encoder_resnet_n_layers_inner,
                        one_hot_inputs=self.data_type.lower() == 'text',
                        oracle_boundary=self.encoder_force_vad_boundaries or self.oracle_boundaries is not None,
                        infer_boundary=self.oracle_boundaries is None,
                        activation=self.encoder_activation,
                        inner_activation=self.encoder_inner_activation,
                        recurrent_activation=self.encoder_recurrent_activation,
                        boundary_activation=self.encoder_boundary_activation,
                        prefinal_activation=self.encoder_prefinal_activation,
                        boundary_discretizer=self.encoder_boundary_discretizer,
                        bottomup_regularizer=self.encoder_weight_regularization,
                        recurrent_regularizer=self.encoder_weight_regularization,
                        topdown_regularizer=self.encoder_weight_regularization,
                        boundary_regularizer=self.encoder_weight_regularization,
                        featurizer_regularizer=self.encoder_weight_regularization,
                        bias_regularizer=None,
                        temporal_dropout=self.temporal_dropout_rate,
                        return_cae=self.correspondence_loss_scale,
                        return_lm_predictions=return_lm_predictions,
                        lm_type=self.lm_loss_type,
                        lm_use_upper=self.lm_use_upper,
                        lm_order_fwd=self.lm_order_fwd,
                        lm_order_bwd=self.lm_order_bwd,
                        temporal_dropout_plug_lm=self.temporal_dropout_plug_lm,
                        bottomup_dropout=self.encoder_dropout,
                        recurrent_dropout=self.encoder_dropout,
                        topdown_dropout=self.encoder_dropout,
                        boundary_dropout=self.encoder_dropout,
                        layer_normalization=self.encoder_layer_normalization,
                        use_bias=self.encoder_use_bias,
                        boundary_slope_annealing_rate=self.boundary_slope_annealing_rate,
                        state_slope_annealing_rate=self.state_slope_annealing_rate,
                        slope_annealing_max=self.slope_annealing_max,
                        min_discretization_prob=self.min_discretization_prob,
                        trainable_self_discretization=self.trainable_self_discretization,
                        state_discretizer=self.encoder_state_discretizer,
                        discretize_state_at_boundary=self.encoder_discretize_state_at_boundary,
                        discretize_final=self.encoder_discretize_final,
                        nested_boundaries=self.nested_boundaries,
                        boundary_noise_level=self.encoder_boundary_noise_level,
                        state_noise_level=self.encoder_state_noise_level,
                        feature_noise_level=self.encoder_feature_noise_level,
                        bottomup_noise_level=self.encoder_bottomup_noise_level,
                        recurrent_noise_level=self.encoder_recurrent_noise_level,
                        topdown_noise_level=self.encoder_topdown_noise_level,
                        sample_at_train=self.sample_at_train,
                        sample_at_eval=self.sample_at_eval,
                        global_step=self.step,
                        implementation=self.encoder_boundary_implementation,
                        decoder_embedding=decoder_embedding,
                        revnet_n_layers=self.encoder_revnet_n_layers,
                        revnet_n_layers_inner=self.encoder_revnet_n_layers_inner,
                        revnet_activation=self.encoder_revnet_activation,
                        revnet_batch_normalization_decay=self.encoder_revnet_batch_normalization_decay,
                        n_passthru_neurons=self.n_passthru_neurons,
                        l2_normalize_states=self.encoder_l2_normalize_states,
                        bptt=self.encoder_bptt,
                        epsilon=self.epsilon,
                        session=self.sess,
                        name='hmlstm_encoder'
                    )

                    # encoder = tf.nn.l2_normalize(encoder, epsilon=self.epsilon, axis=-1)
                    self.segmenter_output = self.segmenter(encoder, mask=mask, boundaries=boundaries)

                    encoder = self.segmenter_output.output(
                        all_layers=self.decoder_concatenate_hidden_states,
                        return_sequences=self.task == 'streaming_autoencoder'
                    )

                    if encoding_batch_normalization_decay:
                        encoder = tf.contrib.layers.batch_norm(
                            encoder,
                            decay=encoding_batch_normalization_decay,
                            center=True,
                            scale=True,
                            zero_debias_moving_mean=True,
                            is_training=self.training,
                            updates_collections=None
                        )

                    self.regularizer_map.update(self.segmenter.get_regularization())

                    self.boundary_slope_coef = self.segmenter.boundary_slope_coef
                    self.state_slope_coef = self.segmenter.state_slope_coef

                    self.segmentation_probs = self.segmenter_output.boundary_probs(as_logits=False, mask=self.X_mask)
                    self.encoder_segmentations = self.segmenter_output.boundary(mask=self.X_mask)
                    self.mean_segmentation_prob = tf.reduce_sum(self.segmentation_probs) / (tf.reduce_sum(self.X_mask) + self.epsilon)

                    if (not self.encoder_boundary_discretizer) or self.segment_at_peaks or self.boundary_prob_discretization_threshold:
                        self.segmentation_probs_smoothed = []
                        self.segmentations = []
                        for l, seg_probs in enumerate(self.segmentation_probs):
                            if self.boundary_prob_discretization_threshold is None:
                                threshold = 0.5
                            else:
                                threshold = self.boundary_prob_discretization_threshold
                            seg_probs_smoothed, segs = self._discretize_seg_probs(
                                seg_probs,
                                self.X_mask,
                                segment_at_peaks=self.segment_at_peaks,
                                threshold=threshold,
                                smoothing=self.boundary_prob_smoothing
                            )

                            self.segmentation_probs_smoothed.append(seg_probs_smoothed)
                            self.segmentations.append(segs)
                    else:
                        self.segmentation_probs_smoothed = None
                        self.segmentations = list(self.encoder_segmentations)
                        if not self.encoder_force_vad_boundaries:
                            for l in range(len(self.segmentations)):
                                segmentations = self.segmentations[l]
                                # Enforce known boundaries
                                fixed_boundaries = self.fixed_boundaries_placeholder
                                if not self.streaming:  # Ensure that the final timestep is a boundary
                                    if self.input_padding == 'pre':
                                        fixed_boundaries = tf.concat(
                                            [self.fixed_boundaries_placeholder[:, :-1],
                                             tf.ones([tf.shape(self.fixed_boundaries_placeholder)[0], 1])],
                                            # [tf.zeros_like(self.fixed_boundaries[:, :-1]), tf.ones([tf.shape(self.fixed_boundaries)[0], 1])],
                                            axis=1
                                        )
                                    else:
                                        right_boundary_ix = tf.cast(tf.reduce_sum(mask, axis=1), dtype=self.INT_TF)
                                        scatter_ix = tf.stack(
                                            [tf.range(tf.shape(segmentations)[0], dtype=self.INT_TF), right_boundary_ix],
                                            axis=1)
                                        updates = tf.ones(tf.shape(segmentations)[0])
                                        shape = tf.shape(segmentations)
                                        right_boundary_marker = tf.scatter_nd(
                                            scatter_ix,
                                            updates,
                                            shape
                                        )
                                        fixed_boundaries = fixed_boundaries + right_boundary_marker

                                segmentations = tf.clip_by_value(segmentations + fixed_boundaries, 0., 1.)
                                self.segmentations[l] = segmentations

                    # Post-process encoder outputs
                    encoder_states_tmp = self.segmenter_output.state(mask=self.X_mask)
                    encoder_cell_states_tmp = self.segmenter_output.cell(mask=self.X_mask)
                    encoder_features_tmp = self.segmenter_output.feature_vectors(mask=self.X_mask)
                    encoder_embeddings_tmp = self.segmenter_output.embedding_vectors(mask=self.X_mask)
                    encoder_feature_targets_tmp = self.segmenter_output.feature_vectors_target(mask=self.X_mask)
                    encoder_states = [x * self.X_mask[..., None] for x in encoder_states_tmp]
                    encoder_cell_states = [x * self.X_mask[..., None] for x in encoder_cell_states_tmp]
                    if self.xent_state_predictions:
                        encoder_features = []
                        encoder_embeddings = []
                        encoder_feature_targets = []
                        for l in range(self.layers_encoder):
                            encoder_features_cur = encoder_features_tmp[l]
                            encoder_embeddings_cur = encoder_embeddings_tmp[l]
                            encoder_features_targets_cur = encoder_feature_targets_tmp[l]
                            if l < (self.layers_encoder - 1 + self.encoder_discretize_final):
                                if not self.encoder_state_discretizer:
                                    encoder_features_cur = ((encoder_features_cur + 1) / 2)
                                    encoder_features_targets_cur = (encoder_features_targets_cur + 1) / 2
                                encoder_features_cur *= self.X_mask[..., None]
                                encoder_embeddings_cur *= self.X_mask[..., None]
                                encoder_features_targets_cur *= self.X_mask[..., None]
                            encoder_features.append(encoder_features_cur)
                            encoder_embeddings.append(encoder_embeddings_cur)
                            encoder_feature_targets.append(encoder_features_targets_cur)
                    else:
                        encoder_features = [x * self.X_mask[..., None] for x in encoder_features_tmp]
                        encoder_embeddings = [x * self.X_mask[..., None] for x in encoder_embeddings_tmp]
                        encoder_feature_targets = [x * self.X_mask[..., None] for x in encoder_feature_targets_tmp]
                    self.encoder_cell_states = encoder_cell_states
                    self.encoder_states = encoder_states
                    self.encoder_features = encoder_features
                    self.encoder_embeddings = encoder_embeddings
                    self.encoder_feature_targets = encoder_feature_targets

                    self.encoder_cell_states = self.segmenter_output.cell(mask=self.X_mask)
                    self.encoder_cell_proposals = self.segmenter_output.cell_proposals(mask=self.X_mask)

                    if self.n_passthru_neurons:
                        self.passthru_neurons = self.segmenter_output.passthru_neurons(mask=self.X_mask)

                    # Regularize encoder outputs
                    for l in range(self.layers_encoder):
                        if l == 0:
                            mask_cur = self.X_mask
                        else:
                            mask_cur = self.encoder_segmentations[l-1]
                        mask_cur_expanded = mask_cur[..., None]

                        denom = tf.reduce_sum(mask_cur)

                        states = self.encoder_states[l]
                        proposals = self.encoder_cell_proposals[l]
                        features = self.encoder_features[l]

                        e = self.epsilon
                        d_s = int(states.shape[-1])
                        d_p = int(proposals.shape[-1])
                        d_f = int(features.shape[-1])

                        mean_states = tf.reduce_sum(states * mask_cur_expanded) / tf.maximum(denom * d_s, e)
                        mean_proposals = tf.reduce_sum(proposals * mask_cur_expanded) / tf.maximum(denom * d_p, e)

                        self._add_regularization(mean_states, self.encoder_state_regularizer)
                        self._add_regularization(mean_proposals, self.encoder_cell_proposal_regularizer)

                        if l < (self.layers_encoder - 1 + self.encoder_discretize_final):
                            mean_features = tf.reduce_sum(features * mask_cur_expanded) / tf.maximum(denom * d_f, e)
                            mean_bitwise_features = tf.reduce_sum(features * mask_cur_expanded, axis=[0,1]) / tf.maximum(denom, e)
                            self._add_regularization(mean_features, self.encoder_feature_regularizer)
                            self._add_regularization(mean_bitwise_features, self.encoder_bitwise_feature_regularizer)

                        if l < (self.layers_encoder - 1):
                            seg_probs = self.segmentation_probs[l]
                            segs = self.encoder_segmentations[l]
                            mean_seg_probs = tf.reduce_sum(seg_probs) / tf.maximum(denom, e)
                            mean_segs = tf.reduce_sum(segs) / tf.maximum(denom, e)
                            self._add_regularization(tf.boolean_mask(seg_probs, mask_cur), self.entropy_regularizer)
                            self._add_regularization(mean_seg_probs, self.boundary_prob_regularizer)
                            self._add_regularization(mean_seg_probs, self.boundary_prob_extremeness_regularizer)
                            self._add_regularization(mean_segs, self.boundary_rate_extremeness_regularizer)
                            self._add_regularization(mean_segs, self.boundary_regularizer)

                    # Compute correspondence tensors
                    if self.correspondence_loss_scale:
                        self.averaged_inputs = self.segmenter_output.averaged_inputs(mask=self.X_mask)
                        self.segment_lengths = self.segmenter_output.segment_lengths(mask=self.X_mask)

                    if self.n_correspondence:
                        self.correspondence_feats_src = []
                        self.correspondence_feats = []
                        self.correspondence_mask = []
                        self.correspondence_hidden_states = []
                        self.correspondence_speaker_ids = []

                        for l in range(self.layers_encoder - 1):
                            correspondence_tensors = self._initialize_correspondence_ae_level(l)

                            self.correspondence_feats_src.append(correspondence_tensors[0])
                            self.correspondence_feats.append(correspondence_tensors[1])
                            self.correspondence_mask.append(correspondence_tensors[2])
                            self.correspondence_hidden_states.append(correspondence_tensors[3])
                            self.correspondence_speaker_ids.append(correspondence_tensors[4])

                    if self.lm_loss_scale:
                        self._initialize_lm()

                elif self.encoder_type.lower() in ['rnn', 'cnn_rnn']:
                    if self.encoder_type == 'cnn_rnn':
                        encoder = Conv1DLayer(
                            self.conv_kernel_size,
                            training=self.training,
                            n_filters=self.frame_dim,
                            padding='same',
                            activation=tf.nn.elu,
                            batch_normalization_decay=self.encoder_batch_normalization_decay,
                            session=self.sess,
                            name='RNN_preCNN'
                        )(encoder)

                    encoder = MultiRNNLayer(
                        training=self.training,
                        units=self.units_encoder[:-1] + [units_utt],
                        layers=self.layers_encoder,
                        activation=self.encoder_inner_activation,
                        inner_activation=self.encoder_inner_activation,
                        recurrent_activation=self.encoder_recurrent_activation,
                        return_sequences=False,
                        name='RNNEncoder',
                        session=self.sess
                    )(encoder, mask=mask)

                    # encoder = DenseLayer(
                    #     training=self.training,
                    #     units=units_utt,
                    #     activation=self.encoder_activation,
                    #     batch_normalization_decay=encoding_batch_normalization_decay,
                    #     session=self.sess
                    # )(encoder)

                    if encoding_batch_normalization_decay:
                        encoder = tf.contrib.layers.batch_norm(
                            encoder,
                            decay=self.encoder_batch_normalization_decay,
                            center=True,
                            scale=True,
                            zero_debias_moving_mean=True,
                            is_training=self.training,
                            updates_collections=None
                        )

                elif self.encoder_type.lower() == 'cnn':
                    for i in range(self.layers_encoder - 1):
                        if i > 0 and self.encoder_resnet_n_layers_inner and self.encoder_resnet_n_layers_inner > 1:
                            encoder = Conv1DResidualLayer(
                                self.conv_kernel_size,
                                training=self.training,
                                n_filters=self.units_encoder[i],
                                padding='causal',
                                layers_inner=self.encoder_resnet_n_layers_inner,
                                activation=self.encoder_inner_activation,
                                activation_inner=self.encoder_inner_activation,
                                batch_normalization_decay=self.batch_normalization_decay,
                                session=self.sess,
                                name='CNNEncoder_l%d' % i
                            )(encoder)
                        else:
                            encoder = Conv1DLayer(
                                self.conv_kernel_size,
                                training=self.training,
                                n_filters=self.units_encoder[i],
                                padding='causal',
                                activation=self.encoder_inner_activation,
                                batch_normalization_decay=self.encoder_batch_normalization_decay,
                                session=self.sess,
                                name='CNNEncoder_l%d' % i
                            )(encoder)

                    encoder = DenseLayer(
                        training=self.training,
                        units=units_utt,
                        activation=self.encoder_activation,
                        batch_normalization_decay=encoding_batch_normalization_decay,
                        session=self.sess,
                        name='CNNEncoder_FC'
                    )(tf.layers.Flatten()(encoder))

                elif self.encoder_type.lower() == 'dense':
                    encoder = tf.layers.Flatten()(encoder)

                    for i in range(self.layers_encoder - 1):
                        if i > 0 and self.encoder_resnet_n_layers_inner and self.encoder_resnet_n_layers_inner > 1:
                            encoder = DenseResidualLayer(
                                training=self.training,
                                units=self.n_timesteps_input * self.units_encoder[i],
                                layers_inner=self.encoder_resnet_n_layers_inner,
                                activation=self.encoder_inner_activation,
                                activation_inner=self.encoder_inner_activation,
                                batch_normalization_decay=self.encoder_batch_normalization_decay,
                                session=self.sess,
                                name='DenseEncoder_l%d' % i
                            )(encoder)
                        else:
                            encoder = DenseLayer(
                                training=self.training,
                                units=self.n_timesteps_input * self.units_encoder[i],
                                activation=self.encoder_inner_activation,
                                batch_normalization_decay=self.encoder_batch_normalization_decay,
                                session=self.sess,
                                name='DenseEncoder_l%d' % i
                            )(encoder)

                    encoder = DenseLayer(
                        training=self.training,
                        units=units_utt,
                        activation=self.encoder_activation,
                        batch_normalization_decay=encoding_batch_normalization_decay,
                        session=self.sess,
                        name='DenseEncoder_l%d' % self.layers_encoder
                    )(encoder)

                else:
                    raise ValueError('Encoder type "%s" is not currently supported' %self.encoder_type)

                return encoder

    def _initialize_lm(self):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if self.lm_loss_type.lower() == 'srn':
                    self._initialize_lm_srn()
                elif self.lm_loss_type.lower() == 'masked_neighbors':
                    self._initialize_lm_masked_neighbors(initialize_decoder = not self.MASKED_NEIGHBORS_OLD)
                else:
                    raise ValueError('Unrecognized lm_loss_type "%s"' % self.lm_loss_type)

    def _postprocess_decoder_logits(self):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                lm_logits = self.segmenter_output.lm_logits(mask=self.X_mask)

                if self.encoder_revnet_n_layers:
                    encoder_lm_revnet_logits = []
                    for l in range(len(lm_logits)):
                        distance_func = self._lm_distance_func(l)
                        if distance_func == 'mse':
                            encoder_lm_revnet_logits_cur = self.segmenter.revnet[l].backward(lm_logits[l])
                        else:
                            encoder_lm_revnet_logits_cur = lm_logits[l]
                        encoder_lm_revnet_logits.append(encoder_lm_revnet_logits_cur)
                    lm_logits = encoder_lm_revnet_logits

                lm_logits_bwd = []
                lm_logits_fwd = []

                for l in range(self.layers_encoder):
                    lm_logits_cur = lm_logits[l]

                    if l == 0:
                        k = int(self.inputs.shape[-1])
                    else:
                        k = int(self.encoder_features[l - 1].shape[-1])

                    order_bwd = self.lm_order_bwd
                    order_fwd = self.lm_order_fwd

                    lm_logits_bwd_cur = lm_logits_cur[..., :k * order_bwd]
                    lm_logits_fwd_cur = lm_logits_cur[..., k * order_bwd:]

                    shape_init = tf.shape(lm_logits_bwd_cur)
                    shape_init = [shape_init[0], shape_init[1]]
                    shape_init_bwd = shape_init + [order_bwd, k * (order_bwd > 0)]
                    shape_init_fwd = shape_init + [order_fwd, k * (order_fwd > 0)]
                    lm_logits_bwd_cur = tf.reshape(lm_logits_bwd_cur, shape_init_bwd)
                    lm_logits_fwd_cur = tf.reshape(lm_logits_fwd_cur, shape_init_fwd)

                    lm_logits_bwd.append(lm_logits_bwd_cur)
                    lm_logits_fwd.append(lm_logits_fwd_cur)

                return lm_logits_bwd, lm_logits_fwd

    def _initialize_lm_srn(self):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                self.lm_logits_bwd, self.lm_logits_fwd = self._postprocess_decoder_logits()

                lm_logits_fwd = []
                lm_targets_bwd = []
                lm_targets_fwd = []
                lm_weights_bwd = []
                lm_weights_fwd = []
                for l in range(self.layers_encoder):
                    distance_func = self._lm_distance_func(l)

                    if l == 0:
                        lm_targets_cur = self.X
                    else:
                        lm_targets_cur = self.encoder_feature_targets[l - 1]

                    # if l > 0 and self.encoder_state_discretizer and self.encoder_discretize_state_at_boundary:
                    #     lm_targets_cur = tf.round(lm_targets_cur)

                    if self.scale_losses_by_boundaries:
                        if l == 0:
                            lm_weights_cur = self.X_mask
                        else:
                            lm_weights_cur = self.encoder_segmentations[l - 1]
                    else:
                        lm_weights_cur = self.X_mask

                    n_lags = self.lm_order_bwd + self.lm_order_fwd

                    lm_targets_bwd_cur = []
                    lm_targets_fwd_cur = []
                    lm_weights_bwd_cur = []
                    lm_weights_fwd_cur = []
                    for i in range(n_lags - 1, -1, -1):
                        s = i
                        e = -(n_lags - i) + 1
                        if e > -1:
                            e = None
                        if i < self.lm_order_bwd:
                            lm_targets_bwd_cur.append(lm_targets_cur[:, s:e])
                            lm_weights_bwd_cur.append(lm_weights_cur[:, s:e])
                        else:
                            lm_targets_fwd_cur.append(lm_targets_cur[:, s:e])
                            lm_weights_fwd_cur.append(lm_weights_cur[:, s:e])

                    if len(lm_targets_bwd_cur) > 0:
                        lm_targets_bwd_cur = tf.stack(lm_targets_bwd_cur, axis=-2)
                    else:
                        lm_targets_bwd_cur = None

                    if len(lm_targets_fwd_cur) > 0:
                        lm_targets_fwd_cur = tf.stack(lm_targets_fwd_cur, axis=-2)
                    else:
                        lm_targets_fwd_cur = None

                    if len(lm_weights_bwd_cur) > 0:
                        lm_weights_bwd_cur = tf.stack(lm_weights_bwd_cur, axis=-1)
                    else:
                        lm_weights_bwd_cur = None

                    if len(lm_weights_fwd_cur) > 0:
                        lm_weights_fwd_cur = tf.stack(lm_weights_fwd_cur, axis=-1)
                    else:
                        lm_weights_fwd_cur = None
                    lm_targets_bwd.append(lm_targets_bwd_cur)
                    lm_targets_fwd.append(lm_targets_fwd_cur)
                    lm_weights_bwd.append(lm_weights_bwd_cur)
                    lm_weights_fwd.append(lm_weights_fwd_cur)

                self.lm_targets_bwd = lm_targets_bwd
                self.lm_targets_fwd = lm_targets_fwd
                self.lm_weights_bwd = lm_weights_bwd
                self.lm_weights_fwd = lm_weights_fwd

                encoder_lm_preds_bwd = None
                encoder_lm_preds_fwd = None
                # 0 is closest in time to input, -1 is furthest in time
                bwd_ix = 0
                fwd_ix = 0
                if self.lm_order_bwd > 0:
                    encoder_lm_preds_bwd = self.lm_logits_bwd[0][..., bwd_ix, :]
                if self.lm_order_fwd > 0:
                    encoder_lm_preds_fwd = self.lm_logits_fwd[0][..., fwd_ix, :]

                if self.data_type.lower() == 'text':
                    if encoder_lm_preds_fwd is not None:
                        encoder_lm_preds_fwd = tf.nn.softmax(encoder_lm_preds_fwd)
                    if encoder_lm_preds_bwd is not None:
                        encoder_lm_preds_bwd = tf.nn.softmax(encoder_lm_preds_bwd)

                self.lm_plot_preds_fwd = encoder_lm_preds_fwd
                self.lm_plot_preds_bwd = encoder_lm_preds_bwd

    def _initialize_lm_masked_neighbors(self, initialize_decoder=True):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                C = 1 # Clipping factor

                b = tf.shape(self.inputs)[0]
                t = tf.shape(self.inputs)[1]
                n_bwd = self.lm_order_bwd
                n_fwd = self.lm_order_fwd

                if not initialize_decoder:
                    logits_bwd_src, logits_fwd_src = self._postprocess_decoder_logits()

                if self.lm_masking_mode.lower() == 'drop_masked':
                    drop_masked = True
                    predict_at_boundaries = True
                elif self.lm_masking_mode.lower() == 'predict_at_boundaries':
                    drop_masked = False
                    predict_at_boundaries = True
                elif self.lm_masking_mode.lower() == 'predict_everywhere':
                    drop_masked = False
                    predict_at_boundaries = False
                else:
                    raise ValueError('Unrecognized value for lm_masking_mode: "%s".' % self.lm_masking_mode)

                decoder_bwd = [None] * self.layers_encoder
                decoder_fwd = [None] * self.layers_encoder
                targets_bwd = [None] * self.layers_encoder
                targets_fwd = [None] * self.layers_encoder
                logits_bwd = [None] * self.layers_encoder
                logits_fwd = [None] * self.layers_encoder
                pe_bwd = [None] * self.layers_encoder
                pe_fwd = [None] * self.layers_encoder
                norm_bwd = [None] * self.layers_encoder
                norm_fwd = [None] * self.layers_encoder
                targets_unnormalized_bwd = [None] * self.layers_encoder
                targets_unnormalized_fwd = [None] * self.layers_encoder
                logits_unnormalized_bwd = [None] * self.layers_encoder
                logits_unnormalized_fwd = [None] * self.layers_encoder
                weights_bwd = [None] * self.layers_encoder
                weights_fwd = [None] * self.layers_encoder
                plot_targs_bwd = [None] * self.layers_encoder
                plot_targs_fwd = [None] * self.layers_encoder
                plot_preds_bwd = [None] * self.layers_encoder
                plot_preds_fwd = [None] * self.layers_encoder
                decoder_attn_bwd = [None] * (self.layers_encoder - 1)
                decoder_attn_fwd = [None] * (self.layers_encoder - 1)
                plot_attn_bwd = [None] * (self.layers_encoder - 1)
                plot_attn_fwd = [None] * (self.layers_encoder - 1)
                plot_attn_keys_bwd = [None] * (self.layers_encoder - 1)
                plot_attn_keys_fwd = [None] * (self.layers_encoder - 1)

                # 0 is closest in time to input, -1 is furthest in time
                ppx_bwd = self.ppx_bwd
                ppx_fwd = self.ppx_fwd

                if self.plot_position_anchor.lower() == 'input':
                    n_plot_bwd = n_plot_fwd = tf.shape(self.inputs)[1]
                elif self.plot_position_anchor.lower() == 'output':
                    n_plot_bwd = n_bwd
                    n_plot_fwd = n_fwd
                else:
                    raise ValueError('Unrecognized plot_position_anchor value %s.' % self.plot_position_anchor)

                if isinstance(ppx_bwd, str) and ppx_bwd.lower() == 'mid':
                    ppx_bwd = n_plot_bwd // 2
                if isinstance(ppx_fwd, str) and ppx_fwd.lower() == 'mid':
                    ppx_fwd = n_plot_fwd // 2

                if initialize_decoder and \
                        self.decoder_positional_encoding_type is not None and \
                        (not self.decoder_type.lower() == 'seq2seqattn' or
                         self.decoder_add_positional_encoding_to_top):
                    pe = construct_positional_encoding(
                        tf.shape(self.X)[1],
                        n_units=self.decoder_positional_encoding_units,
                        n_batch=tf.shape(self.X)[1],
                        positional_encoding_type=self.decoder_positional_encoding_type,
                        positional_encoding_transform=self.decoder_positional_encoding_transform,
                        positional_encoding_activation=self.decoder_positional_encoding_activation,
                        inner_activation=self.decoder_inner_activation,
                        batch_normalization_decay=self.decoder_batch_normalization_decay,
                        conv_kernel_size=self.decoder_conv_kernel_size,
                        training=self.training,
                        name='decoder_positional_encoding',
                        session=self.sess,
                        float_type=self.FLOAT_TF
                    )
                    pe_expanded = pe[None, ...]

                order = {
                    'bwd': n_bwd,
                    'fwd': n_fwd
                }
                direction = ['bwd', 'fwd']

                for l in range(self.layers_encoder - 1, -1, -1):
                    if self.lm_loss_scale[l]:
                        if l == 0:
                            weights_cur = self.X_mask
                            mask_cur = weights_cur
                            targets_cur = self.X
                        else:
                            weights_cur = self.encoder_segmentations[l - 1]
                            if self.backprop_into_loss_weights and self.lm_gradient_scale[l] is not None:
                                weights_cur = replace_gradient(
                                    tf.identity,
                                    lambda x: x * self.lm_gradient_scale[l],
                                    session=self.sess
                                )(weights_cur)
                            if self.round_loss_weights:
                                if self.backprop_into_loss_weights:
                                    weights_cur = round_straight_through(weights_cur, session=self.sess)
                                else:
                                    weights_cur = tf.cast(weights_cur > 0.5, self.FLOAT_TF)
                            elif not self.backprop_into_loss_weights:
                                weights_cur = tf.stop_gradient(weights_cur)
                            if drop_masked:
                                mask_cur = tf.cast(weights_cur > 0.5, self.FLOAT_TF)
                            else:
                                mask_cur = self.X_mask

                            targets_cur = self.encoder_feature_targets[l - 1]
                            if self.lm_target_gradient_scale[l] and self.lm_target_gradient_scale[l] < 1:
                                targets_cur = replace_gradient(
                                    tf.identity,
                                    lambda x: x * self.lm_target_gradient_scale[l],
                                    session=self.sess
                                )(targets_cur)
                            elif not self.lm_target_gradient_scale[l]:
                                targets_cur = tf.stop_gradient(targets_cur)

                            # if l > 0:
                            #     targets_cur = tf.Print(targets_cur, ['l%d' % l, targets_cur], summarize=100)

                        k = int(targets_cur.shape[-1])

                        # if l > 0 and self.encoder_state_discretizer and self.encoder_discretize_state_at_boundary:
                        #     targets_cur = tf.round(targets_cur)

                        lag_dict = mask_and_lag(
                            targets_cur,
                            mask=mask_cur,
                            weights=weights_cur,
                            n_forward=n_fwd,
                            n_backward=n_bwd,
                            session=self.sess
                        )

                        targets_bwd_cur = lag_dict['X_bwd']
                        weights_bwd_cur = lag_dict['weights_bwd']
                        time_ids_bwd_cur = lag_dict['time_ids_bwd']
                        mask_bwd_cur = lag_dict['mask_bwd']
                        targets_fwd_cur = lag_dict['X_fwd']
                        weights_fwd_cur = lag_dict['weights_fwd']
                        time_ids_fwd_cur = lag_dict['time_ids_fwd']
                        mask_fwd_cur = lag_dict['mask_fwd']
                        time_ids_at_pred = lag_dict['time_ids_at_pred']
                        batch_ids_at_pred = lag_dict['batch_ids_at_pred']

                        b_targ = tf.shape(targets_bwd_cur)[0]

                        # if True and l == 0:
                        #     t = tf.cast(tf.range(1, 11), dtype=self.FLOAT_TF)[None, ..., None]
                        #     m = tf.ones([1, 10], dtype=self.FLOAT_TF)
                        #     w = tf.cast(tf.random_uniform([1, 10]) > 0.75, dtype=self.FLOAT_TF)
                        #     m = w
                        #     TB, WB, IB, TF, WF, IF = mask_and_lag(
                        #         t,
                        #         mask=m,
                        #         weights=w,
                        #         n_forward=5,
                        #         n_backward=5,
                        #         session=self.sess
                        #     )
                        #
                        #     print('TB.shape')
                        #     print(TB.shape)
                        #     print('WB.shape')
                        #     print(WB.shape)
                        #     print('IB.shape')
                        #     print(IB.shape)
                        #     print('TF.shape')
                        #     print(TF.shape)
                        #     print('WF.shape')
                        #     print(WF.shape)
                        #     print('IF.shape')
                        #     print(IF.shape)
                        #
                        #     targets_bwd_cur = tf.Print(targets_bwd_cur, ['t', t, 'm', m, 'w', w, 'TB', tf.squeeze(TB, axis=-1), 'WB', WB, 'IB', IB, 'TF', tf.squeeze(TF, axis=-1), 'WF', WF, 'IF', IF], summarize=100)

                        if predict_at_boundaries and not drop_masked:
                            mask_cur = weights_cur
                            weights_masked_cur = tf.boolean_mask(weights_cur, mask_cur)
                            targets_bwd_cur = tf.boolean_mask(targets_bwd_cur, weights_masked_cur)
                            targets_fwd_cur = tf.boolean_mask(targets_fwd_cur, weights_masked_cur)
                            weights_bwd_cur = tf.boolean_mask(weights_bwd_cur, weights_masked_cur)
                            weights_fwd_cur = tf.boolean_mask(weights_fwd_cur, weights_masked_cur)
                            time_ids_bwd_cur = tf.boolean_mask(time_ids_bwd_cur, weights_masked_cur)
                            time_ids_fwd_cur = tf.boolean_mask(time_ids_fwd_cur, weights_masked_cur)

                        if initialize_decoder:
                            # Compute all values needed to initialize the decoder(s)
                            key_encodings = {}
                            key_encodings_cell = {}
                            pe_cur = {}
                            decoder_in = {}
                            decoder_cell_in = {}

                            time_ids = {
                                'bwd': time_ids_bwd_cur,
                                'fwd': time_ids_fwd_cur
                            }

                            if self.decoder_type.lower() == 'seq2seqattn':
                                # Define variables
                                weights = {
                                    'bwd': weights_bwd,
                                    'fwd': weights_fwd
                                }
                                targets = {
                                    'bwd': targets_bwd,
                                    'fwd': targets_fwd
                                }
                                logits = {
                                    'bwd': logits_bwd,
                                    'fwd': logits_fwd
                                }

                                key_val_mask = {}
                                keys_gold = {}
                                keys_pred = {}
                                keys = {}
                                values = {}

                                # Define activation functions
                                if l == 0:
                                    if self.data_type.lower() == 'text':
                                        if self.decoder_discretize_refeed:
                                            output_activation = 'argmax'
                                        else:
                                            output_activation = 'softmax'
                                    else:
                                        if self.backprop_into_refeed:
                                            output_activation = None
                                        else:
                                            output_activation = 'stop_gradient'
                                elif self.xent_state_predictions:
                                    if self.decoder_discretize_refeed:
                                        if self.backprop_into_refeed:
                                            output_activation = 'bsn'
                                        else:
                                            output_activation = lambda x: tf.round(tf.sigmoid(x))
                                    else:
                                        if self.backprop_into_refeed:
                                            output_activation = 'sigmoid'
                                        else:
                                            output_activation = lambda x: tf.stop_gradient(tf.sigmoid(x))
                                else:
                                    if self.backprop_into_refeed:
                                        output_activation = None
                                    else:
                                        output_activation = 'stop_gradient'

                                if self.decoder_use_gold_attn_keys:
                                    key_activation = None
                                else:
                                    if self.decoder_discretize_attn_keys:
                                        if self.backprop_into_attn_keys:
                                            key_activation = 'bsn'
                                        else:
                                            key_activation = lambda x: tf.round(tf.sigmoid(x))
                                    elif self.xent_state_predictions:
                                        if self.backprop_into_attn_keys:
                                            key_activation = 'sigmoid'
                                        else:
                                            key_activation = lambda x: tf.stop_gradient(tf.sigmoid(x))
                                    else:
                                        if self.backprop_into_attn_keys:
                                            key_activation = None
                                        else:
                                            key_activation = 'stop_gradient'

                                key_activation = get_activation(
                                    key_activation,
                                    session=self.sess,
                                    training=self.training,
                                    from_logits=True,
                                    sample_at_train=self.sample_at_train,
                                    sample_at_eval=self.sample_at_eval
                                )

                                # Compute gather indices that align topdown and bottomup sequences
                                if l < self.layers_encoder - 1:
                                    gather_ix = tf.py_func(
                                        align_values_to_batch,
                                        [
                                            batch_ids_at_pred,
                                            time_ids_at_pred,
                                            batch_ids_at_pred_above,
                                            time_ids_at_pred_above
                                        ],
                                        self.INT_TF
                                    )
                                    gather_ix.set_shape([None])
                                else:
                                    gather_ix = None

                                # Compute backward and forward values
                                for x in direction:
                                    if order[x] and l < self.layers_encoder - 1:
                                        pe_cur[x] = tf.zeros(
                                            shape=[b_targ, order[x], 0],
                                            dtype=self.FLOAT_TF
                                        )
                                        key_val_mask[x] = tf.gather(
                                            tf.pad(weights[x][l + 1][:, : order[x] // C], [(0,1), (0,0)]),
                                            gather_ix,
                                            axis=0
                                        )
                                        keys_gold[x] = tf.gather(
                                            tf.pad(targets[x][l + 1][:, : order[x] // C], [(0,1), (0,0), (0,0)]),
                                            gather_ix,
                                            axis=0
                                        )
                                        keys_pred[x] = tf.gather(
                                            tf.pad(logits[x][l + 1][:, : order[x] // C], [(0,1), (0,0), (0,0)]),
                                            gather_ix,
                                            axis=0
                                        )
                                        keys_pred[x] = key_activation(keys_pred[x])

                                        if self.decoder_encode_keys:
                                            encoder_in = keys_gold[x]
                                            if self.lm_gradient_scale[l] is not None:
                                                encoder_in = replace_gradient(
                                                    tf.identity,
                                                    lambda x: x * self.lm_gradient_scale[l],
                                                    session=self.sess
                                                )(encoder_in)

                                            key_encoder = RNNLayer(
                                                training=self.training,
                                                units=self.units_encoder[l],
                                                activation=self.encoder_inner_activation,
                                                recurrent_activation=self.encoder_recurrent_activation,
                                                return_sequences=True,
                                                return_cell_state=True,
                                                batch_normalization_decay=self.decoder_batch_normalization_decay,
                                                name='decoder_key_encoder_%s_l%d' % (x, l),
                                                session=self.sess
                                            )

                                            key_encoder_states, key_encodings_cell[x] = key_encoder(encoder_in, mask=key_val_mask[x])

                                            key_encodings[x] = key_encoder_states[..., -1, :]
                                            # keys[x] = (key_encoder_states * key_val_mask[x][..., None])
                                            # values[x] = keys[x]

                                        else:
                                            key_encodings[x] = None
                                            key_encodings_cell[x] = None

                                        if self.decoder_use_gold_attn_keys:
                                            keys[x] = keys_gold[x]
                                            if self.lm_gradient_scale[l + 1] is not None:
                                                keys[x] = replace_gradient(
                                                    tf.identity,
                                                    lambda x: x * self.lm_gradient_scale[l + 1],
                                                    session=self.sess
                                                )(keys[x])
                                        else:
                                            keys[x] = keys_pred[x]

                                        values[x] = self.segmenter.embedding_fn[l + 1](keys[x])

                                    else:
                                        # Construct decoder input timeseries
                                        # Either a positional encoding or an empty dummy that defines output sequence length
                                        if self.decoder_positional_encoding_type is not None and \
                                                self.decoder_add_positional_encoding_to_top:
                                            if self.decoder_positional_encoding_lock_to_data:
                                                pe_cur[x] = tf.gather(pe, time_ids[x], axis=0)
                                            else:
                                                pe_cur[x] = tf.tile(
                                                    pe_expanded[:, :order[x]],
                                                    [b_targ, 1, 1]
                                                )
                                        else:
                                            pe_cur[x] = tf.zeros(
                                                shape=[b_targ, order[x], 0],
                                                dtype=self.FLOAT_TF
                                            )

                                        # Other attn vars are null
                                        key_val_mask[x] = None
                                        keys_gold[x] = None
                                        keys_pred[x] = None
                                        keys[x] = None
                                        values[x] = None
                                        key_encodings[x] = None
                                        key_encodings_cell[x] = None

                                # Set general decoder kwargs
                                decoder_init_kwargs = {
                                    'training': self.training,
                                    'num_query_units': self.decoder_n_query_units,
                                    'project_keys': self.decoder_project_attn_keys,
                                    'activation': self.decoder_inner_activation,
                                    'recurrent_activation': self.decoder_recurrent_activation,
                                    'output_activation': output_activation,
                                    'sample_at_train': self.sample_at_train,
                                    'sample_at_eval': self.sample_at_eval,
                                    'dtype': self.FLOAT_TF,
                                    'epsilon': self.epsilon,
                                    'session': self.sess
                                }

                                if self.decoder_encode_keys and l < self.layers_encoder - 1:
                                    decoder_in.update(key_encodings)
                                    decoder_cell_in.update(key_encodings_cell)
                                else:
                                    # Select decoder state source
                                    if l == self.layers_encoder - 1:
                                        decoder_in['all'] = self.encoder_states[l]
                                        decoder_cell_in['all'] = self.encoder_cell_states[l]
                                    else:
                                        select = l + self.decoder_initialize_state_from_above
                                        decoder_in['all'] = self.encoder_states[select]
                                        decoder_cell_in['all'] = self.encoder_cell_states[select]

                                    # Rescale gradients
                                    if self.lm_gradient_scale[l] is not None:
                                        decoder_in['all'] = replace_gradient(
                                            tf.identity,
                                            lambda x: x * self.lm_gradient_scale[l],
                                            session=self.sess
                                        )(decoder_in['all'])
                                        decoder_cell_in['all'] = replace_gradient(
                                            tf.identity,
                                            lambda x: x * self.lm_gradient_scale[l],
                                            session=self.sess
                                        )(decoder_cell_in['all'])

                                    # Drop frames
                                    decoder_in['all'] = tf.boolean_mask(decoder_in['all'], mask_cur)
                                    decoder_cell_in['all'] = tf.boolean_mask(decoder_cell_in['all'], mask_cur)

                            else:
                                if self.lm_decode_from_encoder_states:
                                    encoder_features = self.encoder_states
                                else:
                                    encoder_features = self.encoder_embeddings

                                if self.lm_use_upper and l < self.layers_encoder - 1:
                                    if self.lm_boundaries_as_attn:
                                        # Decode using an attention-weighted sum of encoder layers.
                                        # Attention is given to the lowest layer without a boundary.
                                        segs = list(self.encoder_segmentations)
                                        segs = segs[l:]
                                        # attn = []
                                        # attn_mask = None
                                        # for i in range(len(segs) + 1):
                                        #     if i < len(segs):
                                        #         attn_i = (1 - segs[i])
                                        #     else:
                                        #         attn_i = tf.ones(tf.shape(self.encoder_hidden_states[-1])[:-1])
                                        #     if attn_mask is None:
                                        #         attn_mask = 1 - attn_i
                                        #     else:
                                        #         attn_i *= attn_mask
                                        #         attn_mask *= 1 - attn_i
                                        #     attn.append(attn_i)
                                        attn = tf.stack([self.X_mask] + segs, axis=-1)
                                        attn = tf.cumprod(attn, axis=-1)
                                        # attn = tf.Print(attn, [attn, tf.shape(attn)], summarize=100)
                                        decoder_in = encoder_features[l:]
                                        decoder_in = tf.concat([x * attn[..., i:i+1] for i, x in enumerate(decoder_in)], axis=-1)
                                        # decoder_in = tf.Print(decoder_in, [tf.shape(decoder_in)])
                                    else:
                                        decoder_in = tf.concat(encoder_features[l:], axis=-1)
                                        # decoder_in = tf.concat(self.encoder_hidden_states[l:l+1], axis=-1)
                                else:
                                    decoder_in = encoder_features[l]

                                if self.lm_gradient_scale[l] is not None:
                                    decoder_in = replace_gradient(
                                        tf.identity,
                                        lambda x: x * self.lm_gradient_scale[l],
                                        session=self.sess
                                    )(decoder_in)

                                decoder_in = tf.boolean_mask(decoder_in, mask_cur)

                                decoder_in = {
                                    'all': decoder_in
                                }
                                decoder_cell_in = None

                                for x in direction:
                                    if self.decoder_positional_encoding_type is not None:
                                        if self.decoder_positional_encoding_lock_to_data:
                                            pe_cur[x] = tf.gather(pe, time_ids[x], axis=0)
                                        else:
                                            pe_cur[x] = tf.tile(
                                                pe_expanded[:, :order[x]],
                                                [b_targ, 1, 1]
                                            )
                                    else:
                                        pe_cur[x] = None

                                # Set general decoder kwargs
                                decoder_init_kwargs = {
                                    'frame_dim': k,
                                    'decoder_hidden_state_expansion_type': self.decoder_hidden_state_expansion_type,
                                    'decoder_positional_encoding_type': self.decoder_positional_encoding_type,
                                    'decoder_positional_encoding_as_mask': self.decoder_positional_encoding_as_mask,
                                    'decoder_positional_encoding_units': self.decoder_positional_encoding_units,
                                    'decoder_positional_encoding_transform': self.decoder_positional_encoding_transform,
                                    'decoder_positional_encoding_activation': self.decoder_positional_encoding_activation
                                }

                            if l == 0:
                                if self.speaker_emb_dim and self.append_speaker_emb_to_decoder_inputs:
                                    speaker_embeddings = tf.boolean_mask(self.speaker_embeddings, mask_cur)
                                else:
                                    speaker_embeddings = None
                                if self.n_passthru_neurons:
                                    passthru_neurons = tf.boolean_mask(self.passthru_neurons, mask_cur)
                                else:
                                    passthru_neurons = None

                            hidden_units = {}
                            output_units = {}
                            initial_state = {}

                            for x in decoder_in:
                                if l == 0:
                                    in_cur = decoder_in[x]
                                    in_cur = [in_cur]
                                    if self.speaker_emb_dim and self.append_speaker_emb_to_decoder_inputs:
                                        in_cur.append(speaker_embeddings)
                                    if self.n_passthru_neurons:
                                        in_cur.append(passthru_neurons)
                                    if len(in_cur) > 1:
                                        in_cur = tf.concat(in_cur, axis=-1)
                                    else:
                                        in_cur = in_cur[0]
                                    decoder_in[x] = in_cur

                                if self.project_decoder_inputs:
                                    to_project = [decoder_in]
                                    to_project_names = ['in']
                                    if decoder_cell_in is not None:
                                        to_project.append(decoder_cell_in)
                                        to_project_names.append('cell_in')

                                    for y, name in zip(to_project, to_project_names):
                                        in_cur = y[x]
                                        projection_lambdas = []
                                        depth = self.layers_decoder_input_projection
                                        dense_kernel_initializer = 'identity_initializer'
                                        resnet_kernel_initializer = 'glorot_uniform_initializer'

                                        for d in range(depth):
                                            units = self.units_decoder_input_projection[d]
                                            if d == depth - 1:
                                                activation = self.decoder_input_projection_activation
                                            else:
                                                activation = self.decoder_input_projection_activation_inner
                                            name_cur = 'decoder_%s_projection_l%d_d%d' % (name, l, d)

                                            if self.decoder_resnet_n_layers_inner and self.decoder_resnet_n_layers_inner > 1:
                                                kernel_layer = DenseResidualLayer(
                                                    training=self.training,
                                                    units=units,
                                                    kernel_initializer=resnet_kernel_initializer,
                                                    layers_inner=self.decoder_resnet_n_layers_inner,
                                                    activation_inner=activation,
                                                    activation=activation,
                                                    batch_normalization_decay=self.decoder_batch_normalization_decay,
                                                    project_inputs=False,
                                                    session=self.sess,
                                                    name=name_cur
                                                )
                                            else:
                                                kernel_layer = DenseLayer(
                                                    training=self.training,
                                                    units=units,
                                                    kernel_initializer=dense_kernel_initializer,
                                                    activation=activation,
                                                    batch_normalization_decay=self.decoder_batch_normalization_decay,
                                                    session=self.sess,
                                                    reuse=tf.AUTO_REUSE,
                                                    name=name_cur
                                                )

                                            projection_lambdas.append(make_lambda(kernel_layer, session=self.sess))

                                        projection = compose_lambdas(projection_lambdas)

                                        in_cur = projection(in_cur)

                                        y[x] = in_cur

                                hidden_units[x] = decoder_in[x].shape[-1]
                                if l == 0:
                                    output_units[x] = self.X.shape[-1]
                                elif self.features_encoder[l - 1]:
                                    output_units[x] = self.features_encoder[l - 1]
                                else:
                                    output_units[x] = self.units_encoder[l - 1]

                                if decoder_cell_in is not None:
                                    decoder_cell_in[x] = decoder_cell_in[x][..., :hidden_units[x]]

                                    if int(decoder_cell_in[x].shape[-1]) < hidden_units[x]:
                                        decoder_cell_in[x] = tf.pad(
                                            decoder_cell_in[x],
                                            [(0, 0), (hidden_units[x] - int(decoder_cell_in[x].shape[-1]), 0)]
                                        )

                            if self.decoder_type.lower() == 'seq2seqattn':
                                for x in direction:
                                    if self.decoder_encode_keys and l < self.layers_encoder - 1:
                                        states_init = decoder_in[x]
                                        cell_states_init = decoder_cell_in[x]
                                        pred_init = tf.zeros(
                                            shape=[b_targ, output_units[x]],
                                            dtype=self.FLOAT_TF
                                        )
                                    else:
                                        states_init = decoder_in['all']
                                        cell_states_init = decoder_cell_in['all']
                                        pred_init = tf.zeros(
                                            shape=[b_targ, output_units['all']],
                                            dtype=self.FLOAT_TF
                                        )

                                    if l == self.layers_encoder - 1:
                                        t_targ = 0
                                    else:
                                        t_targ = order[x] // C
                                    attn_init = tf.zeros(
                                        shape=[b_targ, t_targ],
                                        dtype=self.FLOAT_TF
                                    )
                                    initial_state[x] = AttentionalLSTMDecoderStateTuple(
                                        h=states_init,
                                        c=cell_states_init,
                                        a=attn_init,
                                        y=pred_init
                                    )

                            # Initialize decoder(s)
                            if n_bwd:
                                if self.decoder_type.lower() == 'seq2seqattn':
                                    if self.decoder_encode_keys and l < self.layers_encoder - 1:
                                        x = 'bwd'
                                    else:
                                        x = 'all'

                                    if self.decoder_positional_encoding_type is not None and \
                                            self.decoder_add_positional_encoding_to_top:
                                        pe_bwd_cur = pe_cur['bwd']
                                    else:
                                        pe_bwd_cur = None

                                    decoder_bwd[l] = AttentionalLSTMDecoderLayer(
                                        hidden_units[x],
                                        output_units[x],
                                        keys=keys['bwd'],
                                        values=values['bwd'],
                                        key_val_mask=key_val_mask['bwd'],
                                        initial_state=initial_state['bwd'],
                                        name='AttentionalDecoder_bwd_l%d' % l,
                                        **decoder_init_kwargs
                                    )

                                    output_bwd_cur = decoder_bwd[l](pe_cur['bwd'])
                                    logits_bwd_cur = output_bwd_cur.y
                                    if l < self.layers_encoder - 1:
                                        decoder_attn_bwd_cur = output_bwd_cur.a * mask_bwd_cur[..., None]
                                        decoder_attn_keys_bwd_cur = decoder_bwd[l].key_matrix * key_val_mask['bwd'][..., None]
                                    else:
                                        decoder_attn_bwd_cur = None
                                        decoder_attn_keys_bwd_cur = None
                                else:
                                    pe_bwd_in = pe_cur['bwd']
                                    logits_bwd_cur, pe_bwd_cur = self._initialize_decoder(
                                        decoder_in['all'],
                                        n_bwd,
                                        mask=weights_bwd_cur,
                                        positional_encoding=pe_bwd_in,
                                        name='decoder_LM_bwd_L%d' % l,
                                        **decoder_init_kwargs
                                    )
                                    pe_bwd_cur *= mask_bwd_cur[..., None]
                                    decoder_attn_bwd_cur = None
                                    decoder_attn_keys_bwd_cur = None

                                logits_bwd_cur *= mask_bwd_cur[..., None]
                            else:
                                logits_bwd_cur = None
                                pe_bwd_cur = None

                            if n_fwd:
                                if self.decoder_type.lower() == 'seq2seqattn':
                                    if self.decoder_encode_keys and l < self.layers_encoder - 1:
                                        x = 'fwd'
                                    else:
                                        x = 'all'

                                    if self.decoder_positional_encoding_type is not None and \
                                            self.decoder_add_positional_encoding_to_top:
                                        pe_fwd_cur = pe_cur['fwd']
                                    else:
                                        pe_fwd_cur = None

                                    decoder_fwd[l] = AttentionalLSTMDecoderLayer(
                                        hidden_units[x],
                                        output_units[x],
                                        keys=keys['fwd'],
                                        values=values['fwd'],
                                        key_val_mask=key_val_mask['fwd'],
                                        initial_state=initial_state['fwd'],
                                        name='AttentionalDecoder_fwd_l%d' % l,
                                        **decoder_init_kwargs
                                    )
                                    output_fwd_cur = decoder_fwd[l](pe_cur['fwd'])
                                    logits_fwd_cur = output_fwd_cur.y
                                    if l < self.layers_encoder - 1:
                                        decoder_attn_fwd_cur = output_fwd_cur.a * mask_fwd_cur[..., None]
                                        decoder_attn_keys_fwd_cur = decoder_fwd[l].key_matrix * key_val_mask['fwd'][..., None]
                                    else:
                                        decoder_attn_fwd_cur = None
                                        decoder_attn_keys_fwd_cur = None
                                else:
                                    pe_fwd_in = pe_cur['fwd']
                                    logits_fwd_cur, pe_fwd_cur = self._initialize_decoder(
                                        decoder_in['all'],
                                        n_fwd,
                                        mask=weights_fwd_cur,
                                        positional_encoding=pe_fwd_in,
                                        name='decoder_LM_fwd_L%d' % l,
                                        **decoder_init_kwargs
                                    )
                                    pe_fwd_cur *= mask_fwd_cur[..., None]
                                    decoder_attn_fwd_cur = None
                                    decoder_attn_keys_fwd_cur = None

                                logits_fwd_cur *= mask_fwd_cur[..., None]
                            else:
                                logits_fwd_cur = None
                                pe_fwd_cur = None

                        else:
                            logits_bwd_cur = tf.boolean_mask(logits_bwd_src[l], mask_cur)
                            logits_fwd_cur = tf.boolean_mask(logits_fwd_src[l], mask_cur)

                        targets_unnormalized_bwd_cur = targets_bwd_cur
                        targets_unnormalized_fwd_cur = targets_fwd_cur
                        if self.l2_normalize_targets and self._lm_distance_func(l) in ['mse', 'cosine', 'arc']:
                            if n_bwd:
                                logits_bwd_cur = tf.nn.l2_normalize(logits_bwd_cur, axis=-1, epsilon=self.epsilon)
                                norm_bwd_cur = tf.norm(targets_bwd_cur, axis=-1, keepdims=True)
                                targets_bwd_cur = tf.nn.l2_normalize(targets_bwd_cur, axis=-1, epsilon=self.epsilon)
                                logits_unnormalized_bwd_cur = logits_bwd_cur * norm_bwd_cur
                            else:
                                norm_bwd_cur = None
                                logits_unnormalized_bwd_cur = None

                            if n_fwd:
                                logits_fwd_cur = tf.nn.l2_normalize(logits_fwd_cur, axis=-1, epsilon=self.epsilon)
                                norm_fwd_cur = tf.norm(targets_fwd_cur, axis=-1, keepdims=True)
                                targets_fwd_cur = tf.nn.l2_normalize(targets_fwd_cur, axis=-1, epsilon=self.epsilon)
                                logits_unnormalized_fwd_cur = logits_fwd_cur * norm_fwd_cur
                            else:
                                norm_fwd_cur = None
                                logits_unnormalized_fwd_cur = None
                        else:
                            norm_bwd_cur = None
                            norm_fwd_cur = None
                            logits_unnormalized_bwd_cur = logits_bwd_cur
                            logits_unnormalized_fwd_cur = logits_fwd_cur

                        # Construct plotting tensors
                        mask_plot = mask_cur
                        scatter_ix = tf.cast(tf.where(mask_plot), dtype=self.INT_TF)

                        plot_targs_bwd_cur = targets_bwd_cur
                        plot_targs_fwd_cur = targets_fwd_cur
                        plot_preds_bwd_cur = logits_bwd_cur
                        plot_preds_fwd_cur = logits_fwd_cur
                        plot_attn_bwd_cur = decoder_attn_bwd_cur
                        plot_attn_fwd_cur = decoder_attn_fwd_cur
                        plot_attn_keys_bwd_cur = decoder_attn_keys_bwd_cur
                        plot_attn_keys_fwd_cur = decoder_attn_keys_fwd_cur

                        if l == 0 and self.data_type.lower() == 'text':
                            if plot_preds_bwd_cur is not None:
                                plot_preds_bwd_cur = tf.nn.softmax(plot_preds_bwd_cur)
                            if plot_preds_fwd_cur is not None:
                                plot_preds_fwd_cur = tf.nn.softmax(plot_preds_fwd_cur)
                        elif l > 0 and self.xent_state_predictions:
                            if plot_preds_bwd_cur is not None:
                                plot_preds_bwd_cur = tf.sigmoid(plot_preds_bwd_cur)
                            if plot_preds_fwd_cur is not None:
                                plot_preds_fwd_cur = tf.sigmoid(plot_preds_fwd_cur)

                        # if n_bwd:
                        #     plot_weights_bwd_cur = weights_bwd_cur[..., None]
                        #     plot_targs_bwd_cur *= plot_weights_bwd_cur
                        #     plot_preds_bwd_cur *= plot_weights_bwd_cur
                        #
                        # if n_fwd:
                        #     plot_weights_fwd_cur = weights_fwd_cur[..., None]
                        #     plot_targs_fwd_cur *= plot_weights_fwd_cur
                        #     plot_preds_fwd_cur *= plot_weights_fwd_cur

                        if initialize_decoder:
                            if n_bwd:
                                plot_preds_bwd_cur = tf.scatter_nd(
                                    scatter_ix,
                                    plot_preds_bwd_cur,
                                    [b, t, n_bwd, tf.shape(plot_preds_bwd_cur)[2]]
                                )
                                plot_targs_bwd_cur = tf.scatter_nd(
                                    scatter_ix,
                                    plot_targs_bwd_cur,
                                    [b, t, n_bwd, tf.shape(plot_targs_bwd_cur)[2]]
                                )
                                if l < self.layers_encoder - 1:
                                    if plot_attn_bwd_cur is not None:
                                        plot_attn_bwd_cur = tf.scatter_nd(
                                            scatter_ix,
                                            plot_attn_bwd_cur,
                                            [b, t, n_bwd, tf.shape(plot_attn_bwd_cur)[2]]
                                        )
                                    if plot_attn_keys_bwd_cur is not None:
                                        plot_attn_keys_bwd_cur = tf.scatter_nd(
                                            scatter_ix,
                                            plot_attn_keys_bwd_cur,
                                            [b, t, n_bwd // C, tf.shape(plot_attn_keys_bwd_cur)[2]]
                                        )

                            if n_fwd:
                                plot_preds_fwd_cur = tf.scatter_nd(
                                    scatter_ix,
                                    plot_preds_fwd_cur,
                                    [b, t, n_fwd, tf.shape(plot_preds_fwd_cur)[2]]
                                )
                                plot_targs_fwd_cur = tf.scatter_nd(
                                    scatter_ix,
                                    plot_targs_fwd_cur,
                                    [b, t, n_fwd, tf.shape(plot_targs_fwd_cur)[2]]
                                )
                                if l < self.layers_encoder - 1:
                                    if plot_attn_fwd_cur is not None:
                                        plot_attn_fwd_cur = tf.scatter_nd(
                                            scatter_ix,
                                            plot_attn_fwd_cur,
                                            [b, t, n_fwd, tf.shape(plot_attn_fwd_cur)[2]]
                                        )
                                    if plot_attn_keys_fwd_cur is not None:
                                        plot_attn_keys_fwd_cur = tf.scatter_nd(
                                            scatter_ix,
                                            plot_attn_keys_fwd_cur,
                                            [b, t, n_fwd // C, tf.shape(plot_attn_keys_fwd_cur)[2]]
                                        )

                        if self.plot_position_anchor.lower() == 'input':
                            if n_bwd:
                                plot_preds_bwd_cur = plot_preds_bwd_cur[:, ppx_bwd, ...]
                                plot_targs_bwd_cur = plot_targs_bwd_cur[:, ppx_bwd, ...]
                                if l < self.layers_encoder - 1:
                                    if plot_attn_bwd_cur is not None:
                                        plot_attn_bwd_cur = plot_attn_bwd_cur[:, ppx_bwd, ...]
                                    if plot_attn_keys_bwd_cur is not None:
                                        plot_attn_keys_bwd_cur = plot_attn_keys_bwd_cur[:, ppx_bwd, ...]
                            if n_fwd:
                                plot_preds_fwd_cur = plot_preds_fwd_cur[:, ppx_fwd, ...]
                                plot_targs_fwd_cur = plot_targs_fwd_cur[:, ppx_fwd, ...]
                                if l < self.layers_encoder - 1:
                                    if plot_attn_fwd_cur is not None:
                                        plot_attn_fwd_cur = plot_attn_fwd_cur[:, ppx_bwd, ...]
                                    if plot_attn_keys_fwd_cur is not None:
                                        plot_attn_keys_fwd_cur = plot_attn_keys_fwd_cur[:, ppx_bwd, ...]
                        else:  # self.plot_position_anchor.lower() == 'output'
                            if n_bwd:
                                plot_preds_bwd_cur = plot_preds_bwd_cur[..., ppx_bwd, :]
                                plot_targs_bwd_cur = plot_targs_bwd_cur[..., ppx_bwd, :]
                                if l < self.layers_encoder - 1:
                                    if plot_attn_bwd_cur is not None:
                                        plot_attn_bwd_cur = plot_attn_bwd_cur[..., ppx_bwd, :]
                                    if plot_attn_keys_bwd_cur is not None:
                                        plot_attn_keys_bwd_cur = plot_attn_keys_bwd_cur[..., ppx_bwd, :]
                            if n_fwd:
                                plot_preds_fwd_cur = plot_preds_fwd_cur[..., ppx_fwd, :]
                                plot_targs_fwd_cur = plot_targs_fwd_cur[..., ppx_fwd, :]
                                if l < self.layers_encoder - 1:
                                    if plot_attn_fwd_cur is not None:
                                        plot_attn_fwd_cur = plot_attn_fwd_cur[..., ppx_fwd, :]
                                    if plot_attn_keys_fwd_cur is not None:
                                        plot_attn_keys_fwd_cur = plot_attn_keys_fwd_cur[..., ppx_fwd, :]

                        targets_bwd[l] = targets_bwd_cur
                        targets_fwd[l] = targets_fwd_cur
                        logits_bwd[l] = logits_bwd_cur
                        logits_fwd[l] = logits_fwd_cur
                        pe_bwd[l] = pe_bwd_cur
                        pe_fwd[l] = pe_fwd_cur
                        norm_bwd[l] = norm_bwd_cur
                        norm_fwd[l] = norm_fwd_cur
                        targets_unnormalized_bwd[l] = targets_unnormalized_bwd_cur
                        targets_unnormalized_fwd[l] = targets_unnormalized_fwd_cur
                        logits_unnormalized_bwd[l] = logits_unnormalized_bwd_cur
                        logits_unnormalized_fwd[l] = logits_unnormalized_fwd_cur
                        weights_bwd[l] = weights_bwd_cur
                        weights_fwd[l] = weights_fwd_cur
                        plot_preds_bwd[l] = plot_preds_bwd_cur
                        plot_preds_fwd[l] = plot_preds_fwd_cur
                        plot_targs_bwd[l] = plot_targs_bwd_cur
                        plot_targs_fwd[l] = plot_targs_fwd_cur
                        if l < self.layers_encoder - 1:
                            decoder_attn_bwd[l] = decoder_attn_bwd_cur
                            decoder_attn_fwd[l] = decoder_attn_fwd_cur
                            plot_attn_bwd[l] = plot_attn_bwd_cur
                            plot_attn_fwd[l] = plot_attn_fwd_cur
                            plot_attn_keys_bwd[l] = plot_attn_keys_bwd_cur
                            plot_attn_keys_fwd[l] = plot_attn_keys_fwd_cur

                        batch_ids_at_pred_above = batch_ids_at_pred
                        time_ids_at_pred_above = time_ids_at_pred

                self.decoder_bwd = decoder_bwd
                self.decoder_fwd = decoder_fwd
                self.lm_targets_bwd = targets_bwd
                self.lm_targets_fwd = targets_fwd
                self.lm_logits_bwd = logits_bwd
                self.lm_logits_fwd = logits_fwd
                self.lm_pe_bwd = pe_bwd
                self.lm_pe_fwd = pe_fwd
                self.lm_norm_bwd = norm_bwd
                self.lm_norm_fwd = norm_fwd
                self.lm_targets_unnormalized_bwd = targets_unnormalized_bwd
                self.lm_targets_unnormalized_fwd = targets_unnormalized_fwd
                self.lm_logits_unnormalized_bwd = logits_unnormalized_bwd
                self.lm_logits_unnormalized_fwd = logits_unnormalized_fwd
                self.lm_weights_bwd = weights_bwd
                self.lm_weights_fwd = weights_fwd
                self.lm_plot_preds_bwd = plot_preds_bwd
                self.lm_plot_preds_fwd = plot_preds_fwd
                self.lm_plot_targs_bwd = plot_targs_bwd
                self.lm_plot_targs_fwd = plot_targs_fwd
                self.decoder_attn_bwd = decoder_attn_bwd
                self.decoder_attn_fwd = decoder_attn_fwd
                self.lm_plot_attn_bwd = plot_attn_bwd
                self.lm_plot_attn_fwd = plot_attn_fwd
                self.lm_plot_attn_keys_bwd = plot_attn_keys_bwd
                self.lm_plot_attn_keys_fwd = plot_attn_keys_fwd

    def _initialize_classifier(self, classifier_in):
        self.encoding = None
        raise NotImplementedError

    def _augment_encoding(self, encoding, encoder=None):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if self.task == 'classifier':
                    if self.binary_classifier:
                        self.labels = binary2integer(tf.round(encoding), session=self.sess)
                        self.label_probs = bernoulli2categorical(encoding, session=self.sess)
                    else:
                        self.labels = tf.argmax(self.encoding, axis=-1)
                        self.label_probs = self.encoding

                extra_dims = None

                if encoder is not None:
                    if self.emb_dim:
                        extra_dims = tf.nn.elu(encoder[:,self.encoding_n_dims:])

                    if self.decoder_use_input_length:
                        utt_len = tf.reduce_sum(self.y_bwd_mask, axis=1, keepdims=True)
                        if self.decoder_use_input_length:
                            if extra_dims is None:
                                extra_dims = utt_len
                            else:
                                extra_dims = tf.concat(
                                [extra_dims, utt_len],
                                axis=1
                            )

                if self.speaker_emb_dim and not self.speaker_revnet_n_layers:
                    speaker_embeddings = self.speaker_embeddings
                    if extra_dims is None:
                        extra_dims = speaker_embeddings
                    else:
                        extra_dims = tf.concat([extra_dims, speaker_embeddings], axis=1)

                if extra_dims is not None:
                    decoder_in = tf.concat([encoding, extra_dims], axis=1)
                else:
                    decoder_in = encoding

                return decoder_in, extra_dims

    def _initialize_decoder(
            self,
            decoder_in,
            n_timesteps,
            frame_dim=None,
            mask=None,
            output_mask=None,
            decoder_hidden_state_expansion_type='tile',
            positional_encoding=None,
            decoder_positional_encoding_type='periodic',
            decoder_positional_encoding_as_mask=False,
            decoder_positional_encoding_units=32,
            decoder_positional_encoding_transform=None,
            decoder_positional_encoding_activation=None,
            name=None
    ):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if name is None:
                    name = 'decoder'
                if frame_dim is None:
                    frame_dim = self.frame_dim

                decoder, positional_encoding, flatten_batch, final_shape, final_shape_positional_encoding = preprocess_decoder_inputs(
                    decoder_in,
                    n_timesteps,
                    self.units_decoder,
                    training=self.training,
                    decoder_hidden_state_expansion_type=decoder_hidden_state_expansion_type,
                    decoder_positional_encoding=positional_encoding,
                    decoder_positional_encoding_type=decoder_positional_encoding_type,
                    decoder_positional_encoding_as_mask=decoder_positional_encoding_as_mask,
                    decoder_positional_encoding_units=decoder_positional_encoding_units,
                    decoder_positional_encoding_transform=decoder_positional_encoding_transform,
                    decoder_inner_activation=self.decoder_inner_activation,
                    decoder_positional_encoding_activation=decoder_positional_encoding_activation,
                    decoder_batch_normalization_decay=self.decoder_batch_normalization_decay,
                    decoder_conv_kernel_size=self.decoder_conv_kernel_size,
                    frame_dim=frame_dim,
                    step=self.step,
                    n_pretrain_steps=self.n_pretrain_steps,
                    name=name,
                    session=self.sess,
                    float_type=self.float_type
                )

                for i in range(self.n_layers_decoder):
                    units_cur = self.units_decoder[i]
                    if i == 0:
                        units_prev = decoder.shape[-1]
                    else:
                        units_prev = self.units_decoder[i - 1]
                    if units_cur != units_prev:
                        project_inputs = True
                    else:
                        project_inputs = False

                    if self.decoder_dropout:
                        decoder = get_dropout(
                            self.decoder_dropout,
                            training=self.training,
                            session=self.sess
                        )(decoder)

                    if i > 0 and self.decoder_resnet_n_layers_inner and self.decoder_resnet_n_layers_inner > 1:
                        if self.decoder_type.lower() == 'rnn':
                            # Possible to-do: implement this using a MaskedLSTMCell
                            decoder = RNNResidualLayer(
                                training=self.training,
                                units=units_cur,
                                layers_inner=self.decoder_resnet_n_layers_inner,
                                activation=self.decoder_inner_activation,
                                activation_inner=self.decoder_inner_activation,
                                recurrent_activation=self.decoder_recurrent_activation,
                                return_sequences=True,
                                batch_normalization_decay=self.decoder_batch_normalization_decay,
                                project_inputs=project_inputs,
                                name=name + '_l%d' % i,
                                session=self.sess
                            )(decoder, mask=mask)
                        elif self.decoder_type.lower() == 'cnn':
                            decoder = Conv1DResidualLayer(
                                self.decoder_conv_kernel_size,
                                training=self.training,
                                n_filters=units_cur,
                                padding='same',
                                layers_inner=self.decoder_resnet_n_layers_inner,
                                activation=self.decoder_inner_activation,
                                activation_inner=self.decoder_inner_activation,
                                batch_normalization_decay=self.decoder_batch_normalization_decay,
                                project_inputs=project_inputs,
                                session=self.sess,
                                name=name + '_l%d' % i
                            )(decoder)
                        elif self.decoder_type.lower() == 'dense':
                            in_shape_flattened, out_shape_unflattened = self._get_decoder_shapes(
                                decoder,
                                n_timesteps,
                                self.units_decoder[i],
                                expand_sequence=False
                            )
                            decoder = tf.reshape(decoder, in_shape_flattened)

                            decoder = DenseResidualLayer(
                                training=self.training,
                                units=n_timesteps * units_cur,
                                layers_inner=self.decoder_resnet_n_layers_inner,
                                activation=self.decoder_inner_activation,
                                activation_inner=self.decoder_inner_activation,
                                project_inputs=project_inputs,
                                batch_normalization_decay=self.decoder_batch_normalization_decay,
                                session=self.sess,
                                name=name + '_l%d' % i
                            )(decoder)

                            decoder = tf.reshape(decoder, out_shape_unflattened)

                    else:
                        if self.decoder_type.lower() == 'rnn':
                            # if self.lm_drop_masked:
                            #     RNN = RNNLayer
                            # else:
                            #     RNN = MaskedLSTMLayer
                            RNN = MaskedLSTMLayer

                            decoder = RNN(
                                training=self.training,
                                units=units_cur,
                                activation=self.decoder_inner_activation,
                                recurrent_activation=self.decoder_recurrent_activation,
                                return_sequences=True,
                                batch_normalization_decay=self.decoder_batch_normalization_decay,
                                name=name + '_l%d' % i,
                                session=self.sess
                            )(decoder, mask=mask)
                        elif self.decoder_type.lower() == 'cnn':
                            decoder = Conv1DLayer(
                                self.decoder_conv_kernel_size,
                                training=self.training,
                                n_filters=units_cur,
                                padding='same',
                                activation=self.decoder_inner_activation,
                                batch_normalization_decay=self.decoder_batch_normalization_decay,
                                session=self.sess,
                                name=name + '_l%d' % i
                            )(decoder)
                        elif self.decoder_type.lower() == 'dense':
                            in_shape_flattened, out_shape_unflattened = self._get_decoder_shapes(
                                decoder,
                                n_timesteps,
                                self.units_decoder[i],
                                expand_sequence=False
                            )
                            decoder = tf.reshape(decoder, in_shape_flattened)

                            decoder = DenseLayer(
                                training=self.training,
                                units=n_timesteps * units_cur,
                                activation=self.decoder_inner_activation,
                                batch_normalization_decay=self.decoder_batch_normalization_decay,
                                session=self.sess,
                                name=name + '_l%d' % i
                            )(decoder)

                            decoder = tf.reshape(decoder, out_shape_unflattened)
                        else:
                            raise ValueError('Decoder type "%s" is not currently supported' %self.decoder_type)

                if self.decoder_dropout:
                    decoder = get_dropout(
                        self.decoder_dropout,
                        training=self.training,
                        session=self.sess
                    )(decoder)

                decoder = DenseLayer(
                    training=self.training,
                    units=frame_dim,
                    activation=self.decoder_activation,
                    batch_normalization_decay=None,
                    name=name + '_final_linear',
                    session=self.sess
                )(decoder)

                # If batch dims were flattened, reshape outputs into expected shape
                if flatten_batch:
                    decoder = tf.reshape(decoder, final_shape)
                    if self.decoder_positional_encoding_type:
                        positional_encoding = tf.reshape(positional_encoding, final_shape_positional_encoding)

                if self.speaker_revnet_n_layers:
                    decoder = self.speaker_revnet.backward(decoder, weights=self.speaker_embeddings)

                if output_mask is not None:
                    decoder *= output_mask[..., None]

                return decoder, positional_encoding

    def _initialize_output_model(self):
        self.out_bwd = None
        raise NotImplementedError

    def _initialize_objective(self, n_train):
        self.reconstructions = None
        self.encoding_post = None
        self.labels = None
        self.labels_post = None
        self.label_probs = None
        self.label_probs_post = None
        raise NotImplementedError

    def _initialize_optimizer(self, name):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                lr = tf.constant(self.learning_rate, dtype=self.FLOAT_TF)
                if name is None:
                    self.lr = lr
                    return None
                if self.lr_decay_family is not None:
                    lr_decay_steps = tf.constant(self.lr_decay_steps, dtype=self.INT_TF)
                    lr_decay_rate = tf.constant(self.lr_decay_rate, dtype=self.FLOAT_TF)
                    lr_decay_staircase = self.lr_decay_staircase

                    if self.lr_decay_iteration_power != 1:
                        t = tf.cast(self.step, dtype=self.FLOAT_TF) ** self.lr_decay_iteration_power
                    else:
                        t = self.step

                    if self.lr_decay_family.lower() == 'linear_decay':
                        if lr_decay_staircase:
                            decay = tf.floor(t / lr_decay_steps)
                        else:
                            decay = t / lr_decay_steps
                        decay *= lr_decay_rate
                        self.lr = lr - decay
                    else:
                        self.lr = getattr(tf.train, self.lr_decay_family)(
                            lr,
                            t,
                            lr_decay_steps,
                            lr_decay_rate,
                            staircase=lr_decay_staircase,
                            name='learning_rate'
                        )
                    if np.isfinite(self.learning_rate_min):
                        lr_min = tf.constant(self.learning_rate_min, dtype=self.FLOAT_TF)
                        INF_TF = tf.constant(np.inf, dtype=self.FLOAT_TF)
                        self.lr = tf.clip_by_value(self.lr, lr_min, INF_TF)
                else:
                    self.lr = lr

                clip = self.max_global_gradient_norm

                return {
                    'SGD': lambda x: self._clipped_optimizer_class(tf.train.GradientDescentOptimizer)(x, max_global_norm=clip) if clip else tf.train.GradientDescentOptimizer(x),
                    'Momentum': lambda x: self._clipped_optimizer_class(tf.train.MomentumOptimizer)(x, 0.9, max_global_norm=clip) if clip else tf.train.MomentumOptimizer(x, 0.9),
                    'AdaGrad': lambda x: self._clipped_optimizer_class(tf.train.AdagradOptimizer)(x, max_global_norm=clip) if clip else tf.train.AdagradOptimizer(x),
                    'AdaDelta': lambda x: self._clipped_optimizer_class(tf.train.AdadeltaOptimizer)(x, max_global_norm=clip) if clip else tf.train.AdadeltaOptimizer(x),
                    'Adam': lambda x: self._clipped_optimizer_class(tf.train.AdamOptimizer)(x, max_global_norm=clip) if clip else tf.train.AdamOptimizer(x),
                    'FTRL': lambda x: self._clipped_optimizer_class(tf.train.FtrlOptimizer)(x, max_global_norm=clip) if clip else tf.train.FtrlOptimizer(x),
                    'RMSProp': lambda x: self._clipped_optimizer_class(tf.train.RMSPropOptimizer)(x, max_global_norm=clip) if clip else tf.train.RMSPropOptimizer(x),
                    'Nadam': lambda x: self._clipped_optimizer_class(tf.contrib.opt.NadamOptimizer)(x, max_global_norm=clip) if clip else tf.contrib.opt.NadamOptimizer(x)
                }[name](self.lr)

    def _initialize_logging(self):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                tf.summary.scalar('objective/loss', self.loss_summary, collections=['objective'])
                tf.summary.scalar('objective/regularizer_loss', self.reg_summary, collections=['objective'])
                if not self.streaming or self.predict_backward:
                    tf.summary.scalar('objective/reconstruction_loss', self.loss_reconstruction_summary, collections=['objective'])
                if self.streaming and self.predict_forward:
                    tf.summary.scalar('objective/prediction_loss', self.loss_prediction_summary, collections=['objective'])
                if self.use_correspondence_loss:
                    for l in range(self.layers_encoder - 1):
                        if self.correspondence_loss_scale[l]:
                            tf.summary.scalar('objective/correspondence_loss_l%d' % (l+1), self.correspondence_loss_summary[l], collections=['objective'])
                if self.lm_loss_scale:
                    for l in range(self.layers_encoder):
                        tf.summary.scalar('objective/encoder_lm_loss_l%d' % (l+1), self.encoder_lm_loss_summary[l], collections=['objective'])
                if self.speaker_adversarial_gradient_scale:
                    for l in range(self.layers_encoder):
                        tf.summary.scalar('objective/encoder_speaker_adversarial_loss_l%d' % (l+1), self.encoder_speaker_adversarial_loss_summary[l], collections=['objective'])
                if self.passthru_adversarial_gradient_scale and self.n_passthru_neurons:
                    for l in range(self.layers_encoder):
                        tf.summary.scalar('objective/encoder_passthru_adversarial_loss_l%d' % (l+1), self.encoder_passthru_adversarial_loss_summary[l], collections=['objective'])

                if self.task.lower() == 'classifier':
                    n_layers = 1
                else:
                    n_layers = self.layers_encoder - 1
                if self.task.lower() == 'classifier':
                    segtypes = [self.segtype]
                elif self.data_type.lower() == 'acoustic':
                    segtypes = ['phn', 'wrd']
                else:
                    segtypes = ['wrd']
                for l in range(n_layers):
                    for s in segtypes:
                        for g in ['goldseg', 'predseg']:
                            for t in ['system', 'random']:
                                tf.summary.scalar('classification_l%d_%s_%s_%s/homogeneity' % (l+1, s, g, t), self.classification_scores[l][s][g][t]['homogeneity'], collections=['classification'])
                                tf.summary.scalar('classification_l%d_%s_%s_%s/completeness' % (l+1, s, g, t), self.classification_scores[l][s][g][t]['completeness'], collections=['classification'])
                                tf.summary.scalar('classification_l%d_%s_%s_%s/v_measure' % (l+1, s, g, t), self.classification_scores[l][s][g][t]['v_measure'], collections=['classification'])
                                tf.summary.scalar('classification_l%d_%s_%s_%s/fmi' % (l+1, s, g, t), self.classification_scores[l][s][g][t]['fmi'], collections=['classification'])

                if 'hmlstm' in self.encoder_type.lower():
                    for l in range(self.layers_encoder - 1):
                        if self.data_type.lower() == 'acoustic':
                            for s in ['phn', 'wrd']:
                                tf.summary.scalar('segmentation_l%d_%s/b_p' %(l+1, s), self.segmentation_scores[l][s]['b_p'], collections=['segmentation'])
                                tf.summary.scalar('segmentation_l%d_%s/b_r' %(l+1, s), self.segmentation_scores[l][s]['b_r'], collections=['segmentation'])
                                tf.summary.scalar('segmentation_l%d_%s/b_f' %(l+1, s), self.segmentation_scores[l][s]['b_f'], collections=['segmentation'])
                                tf.summary.scalar('segmentation_l%d_%s/w_p' % (l+1, s), self.segmentation_scores[l][s]['w_p'], collections=['segmentation'])
                                tf.summary.scalar('segmentation_l%d_%s/w_r' % (l+1, s), self.segmentation_scores[l][s]['w_r'], collections=['segmentation'])
                                tf.summary.scalar('segmentation_l%d_%s/w_f' % (l+1, s), self.segmentation_scores[l][s]['w_f'], collections=['segmentation'])
                        else:
                            s = 'wrd'
                            tf.summary.scalar('segmentation_l%d_%s/b_p' %(l+1, s), self.segmentation_scores[l][s]['b_p'], collections=['segmentation'])
                            tf.summary.scalar('segmentation_l%d_%s/b_r' %(l+1, s), self.segmentation_scores[l][s]['b_r'], collections=['segmentation'])
                            tf.summary.scalar('segmentation_l%d_%s/b_f' %(l+1, s), self.segmentation_scores[l][s]['b_f'], collections=['segmentation'])
                            tf.summary.scalar('segmentation_l%d_%s/w_p' % (l+1, s), self.segmentation_scores[l][s]['w_p'], collections=['segmentation'])
                            tf.summary.scalar('segmentation_l%d_%s/w_r' % (l+1, s), self.segmentation_scores[l][s]['w_r'], collections=['segmentation'])
                            tf.summary.scalar('segmentation_l%d_%s/w_f' % (l+1, s), self.segmentation_scores[l][s]['w_f'], collections=['segmentation'])
                            tf.summary.scalar('segmentation_l%d_%s/l_p' % (l+1, s), self.segmentation_scores[l][s]['l_p'], collections=['segmentation'])
                            tf.summary.scalar('segmentation_l%d_%s/l_r' % (l+1, s), self.segmentation_scores[l][s]['l_r'], collections=['segmentation'])
                            tf.summary.scalar('segmentation_l%d_%s/l_f' % (l+1, s), self.segmentation_scores[l][s]['l_f'], collections=['segmentation'])

                if self.log_graph:
                    self.writer = tf.summary.FileWriter(self.outdir + '/tensorboard/dnnseg', self.sess.graph)
                else:
                    self.writer = tf.summary.FileWriter(self.outdir + '/tensorboard/dnnseg')
                self.summary_objective = tf.summary.merge_all(key='objective')
                self.summary_classification = tf.summary.merge_all(key='classification')
                self.summary_segmentation = tf.summary.merge_all(key='segmentation')

    def _initialize_saver(self):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                self.saver = tf.train.Saver()

                self.check_numerics_ops = [tf.check_numerics(v, 'Numerics check failed') for v in tf.trainable_variables()]

    def _initialize_ema(self):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if self.ema_decay:
                    vars = [var for var in tf.get_collection('trainable_variables') if 'BatchNorm' not in var.name]

                    self.ema = tf.train.ExponentialMovingAverage(decay=self.ema_decay)
                    self.ema_op = self.ema.apply(vars)
                    self.ema_map = {}
                    for v in vars:
                        self.ema_map[self.ema.average_name(v)] = v
                    self.ema_saver = tf.train.Saver(self.ema_map)




    ############################################################
    # Private Soft-DTW methods
    ############################################################

    def _pairwise_distances(self, targets, preds, distance_func='l2norm', dtw_distance=False, gamma=1):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                targets = tf.expand_dims(targets, axis=-2)
                preds = tf.expand_dims(preds, axis=-3)
                if dtw_distance:
                    distances = self._soft_dtw(
                        targets[..., None],
                        preds[..., None],
                        gamma,
                        distance_func='l1norm',
                        dtw_distance=False
                    )
                else:
                    if distance_func.lower() in ['binary_xent', 'softmax_xent']:
                        targets_tile_dims = [1] * (len(targets.shape) - 2) + [preds.shape[-2], 1]
                        targets = tf.tile(targets, targets_tile_dims)

                        preds_tile_dims = [1] * (len(preds.shape) - 3) + [targets.shape[-3], 1, 1]
                        preds = tf.tile(preds, preds_tile_dims)

                        if distance_func.lower() == 'binary_xent':
                            xent = tf.nn.sigmoid_cross_entropy_with_logits
                        else:
                            xent = tf.nn.softmax_cross_entropy_with_logits_v2

                        D = xent(labels=targets, logits=preds)

                        distances = tf.reduce_sum(
                            D,
                            axis=-1
                        )
                    elif distance_func.lower() in ['norm', 'l1', 'l2', 'l1norm', 'l2norm', 'mse']:
                        offsets = targets - preds

                        if distance_func.lower() in ['l1', 'l1norm']:
                            ord = 1
                        elif distance_func.lower() in ['norm', 'l2', 'l2norm', 'mse']:
                            ord = 2
                        else:
                            raise ValueError('Unrecognized distance_func "%s".' % distance_func)

                        distances = tf.norm(offsets, ord=ord, axis=-1)
                    else:
                        raise ValueError('Unrecognized distance_func "%s".' % distance_func)

                return distances

    def _min_smoothed(self, input, gamma, axis=0, keepdims=False):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                # The log(3) is a correction factor (absent from Cuturi & Blondel '18) guaranteeing strictly positive distance
                out = -gamma * tf.reduce_logsumexp(-input / gamma, axis=axis, keepdims=keepdims) + tf.log(3.)
                # with tf.control_dependencies([tf.assert_greater_equal(out, 0., ['negative min smoothed', out, -input / gamma, tf.reduce_logsumexp(-input / gamma, axis=axis, keepdims=keepdims)])]):
                #     out = tf.identity(out)
                return out

    def _dtw_compute_cell(self, D_ij, R_im1_jm1, R_im1_j, R_i_jm1, gamma):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                min_smoothed = self._min_smoothed(tf.stack([R_im1_jm1, R_im1_j, R_i_jm1], axis=0), gamma, axis=0)
                r_ij = D_ij + min_smoothed

                return r_ij

    def _dtw_inner_scan(self, D_i, R_im1_jm1_init, R_im1_j, R_i_jm1_init, gamma):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                # Row above and behind, using initial value for first cell
                R_im1_jm1 = tf.concat([R_im1_jm1_init, R_im1_j[:-1, ...]], axis=0)

                # Scan over columns of D (prediction indices)
                out = tf.scan(
                    lambda a, x: self._dtw_compute_cell(
                        D_ij=x[0],
                        R_im1_jm1=x[1],
                        R_im1_j=x[2],
                        R_i_jm1=a,
                        gamma=gamma
                    ),
                    [D_i, R_im1_jm1, R_im1_j],
                    initializer=R_i_jm1_init,
                    swap_memory=True,
                    parallel_iterations=32
                )

                return out

    def _dtw_outer_scan(self, D, gamma):
        # Scan the rows of the distance matrix D to produce the score matrix R
        with self.sess.as_default():
            with self.sess.graph.as_default():
                # Extract dimensions
                n = tf.shape(D)[0] # Number of target timesteps
                m = tf.shape(D)[1] # Number of pred timesteps
                b = tf.shape(D)[2:] # Batch shape

                R_im1_j_init = tf.fill(tf.concat([[m], b], axis=0), np.inf)
                R_im1_jm1_init = tf.concat(
                    [
                        tf.zeros(tf.concat([[1, 1], b], axis=0), dtype=self.FLOAT_TF),
                        tf.fill(tf.concat([[n-1, 1], b], axis=0), np.inf)
                    ],
                    axis=0
                )
                R_i_jm1_init = tf.fill(b, np.inf)

                # Scan over rows of D (target timesteps)
                out = tf.scan(
                    lambda a, x: self._dtw_inner_scan(
                        D_i=x[0],
                        R_im1_jm1_init=x[1],
                        R_im1_j=a,
                        R_i_jm1_init=R_i_jm1_init,
                        gamma=gamma
                    ),
                    [D, R_im1_jm1_init],
                    initializer=R_im1_j_init,
                    swap_memory=True,
                    parallel_iterations=32
                )

                return out

    def _soft_dtw(self, targets, preds, gamma, mask=None, weights_targets=None, weights_preds=None, distance_func='norm', dtw_distance=False):
        # Outer scan over n target timesteps
        # Inner scan over m pred timesteps

        with self.sess.as_default():
            with self.sess.graph.as_default():
                # Get number of timesteps
                if mask is None:
                    end = -1
                else:
                    end = tf.cast(tf.reduce_sum(mask, axis=-1), dtype=self.INT_TF)

                # Compute distance matrix
                D = self._pairwise_distances(targets, preds, distance_func=distance_func, dtw_distance=dtw_distance)

                # Rescale distances by target weights
                if weights_targets is not None:
                    while len(weights_targets.shape) < len(D.shape):
                        weights_targets = weights_targets[None, ...]
                    D *= weights_targets

                # Rescale distances by prediction weights
                if weights_preds is not None:
                    while len(weights_preds.shape) < len(D.shape):
                        weights_preds = weights_preds[None, ...]
                    D *= weights_preds

                # Move time dimensions to beginning so we can scan along them
                perm = list(range(len(D.shape)))
                perm = perm[-2:] + perm[:-2]
                D = tf.transpose(D, perm=perm)

                # Perform soft-DTW alignment
                R = self._dtw_outer_scan(D, gamma)

                # Move time dimensions back to end to match input shape
                perm = list(range(len(D.shape)))
                perm = perm[2:] + perm[:2]
                R = tf.transpose(R, perm=perm)

                gather_ix = tf.stack([tf.range(tf.shape(end)[0]), tf.maximum(end - 1, 0)], axis=1)
                out = R
                out = tf.gather_nd(out, gather_ix)
                out = tf.gather_nd(out, gather_ix)

                return out

    # Numpy sanity checks for Soft-DTW implementation

    def _logsumexp_NP(self, input, axis=-1):
        max_in = np.max(input, axis=axis)
        ds = input - max_in[..., None]
        sum_of_exp = np.exp(ds).sum(axis=axis)
        return max_in + np.log(sum_of_exp)

    def _min_smoothed_NP(self, input, gamma, axis=-1, keepdims=False):
        out = -input / gamma
        out = -gamma * self._logsumexp_NP(out, axis=axis)
        return out

    def _pairwise_distances_NP(self, targets, preds):
        targets = np.expand_dims(targets, axis=2)
        preds = np.expand_dims(preds, axis=1)
        distances = targets - preds

        out = np.linalg.norm(distances, axis=3)

        return out

    def _soft_dtw_NP(self, targets, preds, gamma, targets_mask=None, preds_mask=None):
        if targets_mask is None:
            targets_mask = np.ones(targets.shape[:-1])
        if preds_mask is None:
            preds_mask = np.ones(preds.shape[:-1])
        targets_mask = targets_mask.astype(np.bool)
        preds_mask = preds_mask.astype(np.bool)

        D = self._pairwise_distances_NP(targets, preds)
        n = int(D.shape[1])
        m = int(D.shape[2])

        R = np.zeros((D.shape[0], D.shape[1] + 1, D.shape[2] + 1))

        inf = np.full((D.shape[0],), np.inf)
        zero = np.zeros((D.shape[0],))

        R[:, 1:, 0] = inf
        R[:, 0, 1:] = inf

        for j in range(1, m+1):
            for i in range(1, n+1):
                r_ij = D[:,i-1,j-1] + self._min_smoothed_NP(np.stack([R[:, i - 1, j - 1], R[:, i - 1, j], R[:, i, j - 1]], axis=1), gamma, axis=1)
                R[:,i,j] = np.where(
                    np.logical_and(targets_mask[:,i-1], preds_mask[:,j-1]),
                    r_ij,
                    zero
                )

        return R





    ############################################################
    # Private utility methods
    ############################################################

    def _resample_signal(
            self,
            x,
            num
    ):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                X = tf.cast(x, dtype=tf.complex64)

                X = tf.fft(
                    X
                )

                shape = tf.shape(x)
                Nx = shape[-1]
                new_shape = []
                for i in range(len(x.shape) - 1):
                    new_shape.append(shape[i])
                new_shape.append(num)

                sl = [slice(None)] * len(x.shape)

                N = tf.minimum(num, Nx)

                sl[-1] = slice(0, (N + 1) // 2)
                y_1 = X[sl]

                sl[-1] = slice(-(N - 1) // 2, None)
                y_2 = X[sl]

                Ndiff = num - (tf.shape(y_1)[-1] + tf.shape(y_2)[-1])

                y = tf.cond(
                    Ndiff > 0,
                    lambda: tf.concat([y_1, tf.zeros(new_shape[:-1] + [Ndiff],dtype=tf.complex64), y_2], axis=-1),
                    lambda: tf.concat([y_1, y_2], axis=-1)
                )

                y = tf.ifft(y)

                y = tf.cast(y, dtype=self.FLOAT_TF)

                return y

    def _extract_backward_targets(self, n):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                pass

    ## Thanks to Keisuke Fujii (https://github.com/blei-lab/edward/issues/708) for this idea
    def _clipped_optimizer_class(self, base_optimizer):
        class ClippedOptimizer(base_optimizer):
            def __init__(self, *args, max_global_norm=None, **kwargs):
                super(ClippedOptimizer, self).__init__( *args, **kwargs)
                self.max_global_norm = max_global_norm

            def compute_gradients(self, *args, **kwargs):
                grads_and_vars = super(ClippedOptimizer, self).compute_gradients(*args, **kwargs)
                if self.max_global_norm is None:
                    return grads_and_vars
                grads = tf.clip_by_global_norm([g for g, _ in grads_and_vars], self.max_global_norm)[0]
                vars = [v for _, v in grads_and_vars]
                grads_and_vars = []
                for grad, var in zip(grads, vars):
                    grads_and_vars.append((grad, var))
                return grads_and_vars

            def apply_gradients(self, grads_and_vars, **kwargs):
                if self.max_global_norm is None:
                    return grads_and_vars
                grads, _ = tf.clip_by_global_norm([g for g, _ in grads_and_vars], self.max_global_norm)
                vars = [v for _, v in grads_and_vars]
                grads_and_vars = []
                for grad, var in zip(grads, vars):
                    grads_and_vars.append((grad, var))

                return super(ClippedOptimizer, self).apply_gradients(grads_and_vars, **kwargs)

        return ClippedOptimizer

    def _add_regularization(self, var, regularizer):
        if regularizer is not None:
            with self.sess.as_default():
                with self.sess.graph.as_default():
                    self.regularizer_map[var] = regularizer

    def _apply_regularization(self, normalized=False):
        regularizer_losses = []
        # denom = 0
        for var in self.regularizer_map:
            reg_loss = tf.reduce_sum(tf.contrib.layers.apply_regularization(self.regularizer_map[var], [var]))
            if normalized:
                # Normalize the loss by dividing by the number of cells in var
                # Makes regularization scales comparable across tensors of different sizes
                reg_loss /= tf.maximum(tf.cast(tf.reduce_prod(tf.shape(var)), self.FLOAT_TF), self.epsilon)
                # denom += 1
            regularizer_losses.append(reg_loss)

        regularizer_loss = tf.add_n(regularizer_losses)

        # if normalized and denom > 0:
        #     regularizer_loss /= denom

        return regularizer_loss

    def _regularize_correspondences(self, layer_number, preds):
        if self.regularize_correspondences:
            with self.sess.as_default():
                with self.sess.graph.as_default():
                    states = self.encoder_features[layer_number]
                    if self.reverse_targets:
                        states = states[:, ::-1, :]

                    if self.matched_correspondences:
                        correspondences = states - preds
                        self._add_regularization(
                            correspondences,
                            tf.contrib.layers.l2_regularizer(self.segment_encoding_correspondence_regularizer_scale)
                        )
                    else:
                        correspondences = self._soft_dtw(states, preds, self.dtw_gamma)
                        self._add_regularization(
                            correspondences,
                            tf.contrib.layers.l1_regularizer(self.segment_encoding_correspondence_regularizer_scale)
                        )

    def _get_decoder_shapes(self, decoder, n_timesteps, units, expand_sequence=False):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                decoder_in_shape = tf.shape(decoder)
                if expand_sequence:
                    decoder_in_shape_flattened = decoder_in_shape
                else:
                    feat = int(decoder.shape[-1])
                    decoder_in_shape_flattened = tf.concat([decoder_in_shape[:-2], [n_timesteps * feat]], axis=0)
                decoder_out_shape = tf.concat([decoder_in_shape_flattened[:-1], [n_timesteps, units]], axis=0)

                return decoder_in_shape_flattened, decoder_out_shape

    def _streaming_dynamic_scan_inner(self, targets, prev, encoder_in, left, cur, right):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                prev_loss, state, _, _ = prev
                if self.batch_normalize_encodings:
                    encoding_batch_normalization_decay = self.encoder_batch_normalization_decay
                else:
                    encoding_batch_normalization_decay = None

                units_utt = self.units_encoder[-1]
                if self.emb_dim:
                    units_utt += self.emb_dim

                log_loss = self.data_normalization == 'range' and self.constrain_output

                o, h = self.encoder_cell(encoder_in, state)
                encoder = o[-1][0]
                encoder = DenseLayer(
                    self.training,
                    units=units_utt,
                    activation=self.encoder_activation,
                    batch_normalization_decay=encoding_batch_normalization_decay,
                    session=self.sess
                )(encoder)

                encoding = self._initialize_classifier(encoder)
                decoder_in, extra_dims = self._augment_encoding(encoding, encoder=encoder)

                targ_left = targets[cur:left:-1]
                with tf.variable_scope('left'):
                    if self.mask_padding:
                        mask = self.y_bwd_mask
                    else:
                        mask = None
                    pred_left = self._initialize_decoder(
                        decoder_in,
                        self.window_len_bwd,
                        mask=mask,
                        decoder_hidden_state_expansion_type=self.decoder_hidden_state_expansion_type,
                        decoder_positional_encoding_type=self.decoder_positional_encoding_type,
                        decoder_positional_encoding_as_mask=self.decoder_positional_encoding_as_mask,
                        decoder_positional_encoding_units=self.decoder_positional_encoding_units,
                        decoder_positional_encoding_transform=self.decoder_positional_encoding_transform,
                        decoder_positional_encoding_activation=self.decoder_positional_encoding_activation,
                    )
                with tf.variable_scope('right'):
                    if self.mask_padding:
                        mask = self.y_fwd_mask
                    else:
                        mask = None
                    pred_right = self._initialize_decoder(
                        decoder_in,
                        self.window_len_fwd,
                        decoder_hidden_state_expansion_type=self.decoder_hidden_state_expansion_type,
                        decoder_positional_encoding_type=self.decoder_positional_encoding_type,
                        decoder_positional_encoding_as_mask=self.decoder_positional_encoding_as_mask,
                        decoder_positional_encoding_units=self.decoder_positional_encoding_units,
                        decoder_positional_encoding_transform=self.decoder_positional_encoding_transform,
                        decoder_positional_encoding_activation=self.decoder_positional_encoding_activation,
                        mask=mask
                    )

                loss_left = self._get_loss(targ_left, pred_left, distance_func=log_loss)
                loss_left /= self.window_len_bwd

                targ_right = targets[cur + 1:right + 1]
                loss_right = self._get_loss(targ_right, pred_right, distance_func=log_loss)
                loss_right /= self.window_len_fwd

                loss = loss_left + loss_right

                return (prev_loss + loss, h, pred_left, pred_right)

    def _streaming_dynamic_scan(self, input, reset_state=False):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                # Make input time major
                batch_size = tf.shape(input)[0]
                n_timesteps = tf.shape(input)[1]
                input = tf.transpose(input, [1, 0, 2])

                targets = self.X
                if not self.predict_deltas:
                    targets = targets[..., :self.n_coef]
                targets = tf.pad(targets, [[0, 0], [self.window_len_bwd, self.window_len_fwd], [0, 0]])
                targets = tf.transpose(targets, [1, 0, 2])
                t_lb = tf.range(0, tf.shape(self.X)[1])
                t = t_lb + self.window_len_bwd
                t_rb = t + self.window_len_fwd

                losses, states, preds_left, preds_right = tf.scan(
                    lambda a, x: self._streaming_dynamic_scan_inner(targets, a, x[0], x[1], x[2], x[3]),
                    [input, t_lb, t, t_rb],
                    initializer=(
                        0., # Initial loss
                        self.encoder_state, # Initial state
                        tf.zeros([batch_size, self.window_len_bwd, self.frame_dim]), # Initial left prediction
                        tf.zeros([batch_size, self.window_len_fwd, self.frame_dim]) # Initial right prediction
                    ),
                    swap_memory=True,
                    parallel_iterations=32
                )

                return losses[-1], states, preds_left, preds_right

    def _map_state(self, state):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                fd = {}
                for i in range(len(self.encoder_state)):
                    for j in range(len(self.encoder_state[i])):
                        fd[self.encoder_state[i][j]] = state[i][j]

                return fd

    def _get_segs_and_states(self, X, X_mask, training=False):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if self.pad_seqs:
                    if not np.isfinite(self.eval_minibatch_size):
                        minibatch_size = len(X)
                    else:
                        minibatch_size = self.eval_minibatch_size
                else:
                    minibatch_size = 1

                segmentation_probs = []
                segmentations = []
                states = []

                for i in range(0, len(X), minibatch_size):
                    if self.pad_seqs:
                        indices = slice(i, i + minibatch_size, 1)
                    else:
                        indices = i

                    fd_minibatch = {
                        self.X: X[indices],
                        self.X_mask: X_mask[indices],
                        self.training: training
                    }

                    segmentation_probs_cur, segmentations_cur, states_cur = self.sess.run(
                        [self.segmentation_probs, self.segmentation_probs, self.encoder_features[:-1]],
                        feed_dict=fd_minibatch
                    )

                    segmentation_probs.append(np.stack(segmentation_probs_cur, axis=-1))
                    segmentations.append(np.stack(segmentations_cur, axis=-1))
                    states.append(np.concatenate(states_cur, axis=-1))

                new_segmentation_probs = [np.squeeze(x, axis=-1) for x in np.split(np.concatenate(segmentation_probs, axis=0), 1, axis=-1)]
                new_segmentations = [np.squeeze(x, axis=-1) for x in np.split(np.concatenate(segmentations, axis=0), 1, axis=-1)]
                new_states = np.split(np.concatenate(states, axis=0), np.cumsum(self.units_encoder[:-1], dtype='int'), axis=-1)

                return new_segmentation_probs, new_segmentations, new_states

    def _collect_previous_segments(self, n, data, segtype=None, X=None, X_mask=None, training=True):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if segtype is None:
                    segtype = self.segtype

                if X is None or X_mask is None:
                    stderr('Getting input data...\n')
                    X, X_mask, _ = data.inputs(
                        segments=segtype,
                        padding=self.input_padding,
                        normalization=self.data_normalization,
                        center=self.center_data,
                        resample=self.resample_inputs,
                        max_len=self.max_len
                    )

                stderr('Collecting boundary and state predictions...\n')
                segmentation_probs, segmentations, states = self._get_segs_and_states(X, X_mask, training=training)

                if 'bsn' in self.encoder_boundary_activation.lower():
                    smoothing_algorithm = None
                else:
                    smoothing_algorithm = 'rbf'

                stderr('Converting predictions into tables of segments...\n')
                segment_tables = data.get_segment_tables(
                    segmentations,
                    parent_segment_type=segtype,
                    states=states,
                    smoothing_algorithm=smoothing_algorithm,
                    mask=X_mask,
                    padding=self.input_padding
                )


                stderr('Resegmenting input data...\n')
                y = []

                for l in range(len(segment_tables)):
                    n_segments = 0
                    for x in segment_tables[l]:
                        n_segments += len(segment_tables[l][x])
                    select_ix = np.random.permutation(np.arange(0, n_segments))[:n]
                    select = np.zeros(n_segments)
                    select[select_ix] = 1
                    select = select.astype('bool')

                    i = 0
                    for f in data.fileIDs:
                        n_segments_cur = len(segment_tables[l][f])
                        select_cur = select[i:i+n_segments_cur]
                        segment_tables[l][f] = segment_tables[l][f][select_cur]
                        i += n_segments_cur

                    y_cur, _, _ = data.targets(
                        segments=segment_tables[l],
                        padding='post',
                        reverse=self.reverse_targets,
                        normalization=self.data_normalization,
                        center=self.center_data,
                        with_deltas=self.predict_deltas,
                        resample=self.resample_correspondence
                    )

                    y.append(y_cur)

                stderr('Collecting segment embeddings...\n')
                n_units = self.units_encoder
                embeddings = []
                for l in range(len(segment_tables)):
                    embeddings_cur = []
                    for f in data.fileIDs:
                        embeddings_cur.append(segment_tables[l][f][['d%d' %u for u in range(n_units[l])]].as_matrix())
                    embeddings.append(np.concatenate(embeddings_cur, axis=0))

                return embeddings, y

    def process_previous_segments(
            self,
            correspondence_seg_feats,
            correspondence_seg_feats_mask
    ):
        n = len(correspondence_seg_feats)
        correspondence_seg_feats = np.split(correspondence_seg_feats, n)
        correspondence_seg_feats_mask = np.split(correspondence_seg_feats_mask.astype('bool'), n)

        correspondence_seg_feats_out = []
        for i in range(len(correspondence_seg_feats)):
            feats_cur = correspondence_seg_feats[i][correspondence_seg_feats_mask[i]]
            if self.resample_correspondence:
                feats_cur = scipy.signal.resample(feats_cur, self.resample_correspondence, axis=0)
            correspondence_seg_feats_out.append(feats_cur)

        if self.resample_correspondence:
            correspondence_seg_feats_out = np.stack(correspondence_seg_feats_out, axis=0)

        return correspondence_seg_feats_out

    def _discretize_seg_probs(
            self,
            seg_probs,
            mask,
            segment_at_peaks=True,
            threshold=0.5,
            smoothing=None
    ):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                # Discretize the segmentation probs by fitting a smooth spline to them
                # and finding indices of peaks

                if smoothing:
                    smoothing_parsed = smoothing.split('_')
                    smoothing_type = smoothing_parsed[0]
                    smoothing_args = smoothing_parsed[1:]

                    if smoothing_type == 'ema':
                        assert len(smoothing_args) == 1, 'EMA smooth must have exactly 1 argument: <decay>.'
                        try:
                            decay = float(smoothing_args[0])
                            assert decay >= 0 and decay <= 1, 'First argument of EMA smooth must be a decay in the interval [0,1].'
                        except ValueError:
                            raise ValueError('Second argument of EMA smooth must be a float in the interval [0,1]')

                        if self.input_padding == 'pre':
                            mask_flipped = 1 - mask
                            start_time_ix = tf.cast(tf.reduce_sum(mask_flipped, axis=-1), self.INT_TF)
                            gather_ix = tf.stack([tf.range(tf.shape(start_time_ix)[0]), start_time_ix], axis=-1)
                            start_vals = tf.gather_nd(
                                seg_probs,
                                gather_ix
                            )
                            seg_probs += mask_flipped * start_vals[..., None]

                        seg_probs = ema(seg_probs, axis=-1, decay=decay, session=self.sess)

                    elif smoothing_type == 'dema':
                        assert len(smoothing_args) == 1, 'DEMA smooth must have exactly 1 argument: <decay>.'
                        try:
                            decay = float(smoothing_args[0])
                            assert decay >= 0 and decay <= 1, 'First argument of DEMA smooth must be a decay in the interval [0,1].'
                        except ValueError:
                            raise ValueError('First argument of DEMA smooth must be a float in the interval [0,1]')

                        if self.input_padding == 'pre':
                            mask_flipped = 1 - mask
                            start_time_ix = tf.cast(tf.reduce_sum(mask_flipped, axis=-1), self.INT_TF)
                            gather_ix = tf.stack([tf.range(tf.shape(start_time_ix)[0]), start_time_ix], axis=-1)
                            start_vals = tf.gather_nd(
                                seg_probs,
                                gather_ix
                            )
                            seg_probs += mask_flipped * start_vals[..., None]

                        seg_probs = dema(seg_probs, axis=-1, decay=decay, session=self.sess)

                    elif smoothing_type == 'wma':
                        assert len(smoothing_args) == 1, 'WMA smooth must have exactly 1 argument: <width>.'
                        try:
                            filter_width = int(smoothing_args[0])
                            assert filter_width >= 0, 'First argument of WMA smooth must be a positive integer.'
                        except ValueError:
                            raise ValueError('First argument of WMA smooth must be a positive integer.')

                        if self.input_padding == 'pre':
                            mask_flipped = 1 - mask
                            start_time_ix = tf.cast(tf.reduce_sum(mask_flipped, axis=-1), self.INT_TF)
                            gather_ix = tf.stack([tf.range(tf.shape(start_time_ix)[0]), start_time_ix], axis=-1)
                            start_vals = tf.gather_nd(
                                seg_probs,
                                gather_ix
                            )
                            seg_probs += mask_flipped * start_vals[..., None]

                        seg_probs = wma(seg_probs, filter_width=filter_width, session=self.sess)

                    else:
                        raise ValueError('Unrecognized smoothing algorithm "%s"' % smoothing_type)

                # Enforce known boundaries
                fixed_boundaries = self.fixed_boundaries_placeholder
                if not self.streaming: # Ensure that the final timestep is a boundary
                    if self.input_padding == 'pre':
                        fixed_boundaries = tf.concat(
                            [self.fixed_boundaries_placeholder[:, :-1], tf.ones([tf.shape(self.fixed_boundaries_placeholder)[0], 1])],
                            # [tf.zeros_like(self.fixed_boundaries[:, :-1]), tf.ones([tf.shape(self.fixed_boundaries)[0], 1])],
                            axis=1
                        )
                    else:
                        right_boundary_ix = tf.cast(tf.reduce_sum(mask, axis=1), dtype=self.INT_TF)
                        scatter_ix = tf.stack(
                            [tf.range(tf.shape(seg_probs)[0], dtype=self.INT_TF), right_boundary_ix], axis=1)
                        updates = tf.ones(tf.shape(seg_probs)[0])
                        shape = tf.shape(seg_probs)
                        right_boundary_marker = tf.scatter_nd(
                            scatter_ix,
                            updates,
                            shape
                        )
                        fixed_boundaries = fixed_boundaries + right_boundary_marker
                    # fixed_boundaries = right_boundary_marker

                seg_probs_with_known_bounds = tf.clip_by_value(seg_probs + fixed_boundaries, 0., 1.)

                zero_pad = tf.zeros([tf.shape(seg_probs_with_known_bounds)[0], 1])
                seg_probs_with_known_bounds = tf.concat([zero_pad, seg_probs_with_known_bounds, zero_pad], axis=1)

                tm1 = seg_probs_with_known_bounds[:, :-2]
                t = seg_probs_with_known_bounds[:, 1:-1]
                tp1 = seg_probs_with_known_bounds[:, 2:]

                if segment_at_peaks:
                    segs = tf.logical_and(t >= tm1, t > tp1)
                    if threshold:
                        segs = tf.logical_and(
                            segs,
                            t >= threshold
                        )
                else:
                    segs = t >= threshold
                segs = tf.cast(
                    segs,
                    dtype=self.FLOAT_TF
                )
                segs *= mask

                return seg_probs, segs

    def _initialize_correspondence_ae_level(
            self,
            l,
    ):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                # Find indices of segments.
                # Returns a tensor of shape (num_found, 2), where the two values of dim 1 are (batch_ix, time_ix)
                segs = self.segmentations[l]
                seg_ix = tf.cast(tf.where(segs), dtype=self.INT_TF)

                # Create masks for acoustic spans of sampled segments
                # Assumes that the tf.where op used to compute seg_ix returns hits
                # in batch+time order, which is true as of TF 1.6 but may not be guaranteed indefinitely
                # NOTE TO SELF: Check in future versions
                batch_ix = seg_ix[:, 0]
                time_ix = seg_ix[:, 1]
                utt_mask = tf.gather(self.X_mask, batch_ix)
                if self.input_padding == 'pre':
                    utt_start_ix = tf.cast(tf.reduce_sum(1 - utt_mask, axis=1), dtype=self.INT_TF)
                else:
                    utt_start_ix = tf.zeros_like(batch_ix, dtype=self.INT_TF)
                seg_same_utt = tf.equal(batch_ix, tf.concat([[-1], batch_ix[:-1]], axis=0))
                start_ix = tf.where(
                    seg_same_utt,
                    tf.concat([utt_start_ix[:1], time_ix[:-1]], axis=0),
                    utt_start_ix
                )[..., None]
                end_ix = time_ix[..., None] + 1

                # Randomly sample discovered segments with replacement
                correspondence_ix = tf.random_uniform(
                    [self.n_correspondence],
                    maxval=tf.shape(seg_ix)[0],
                    dtype=self.INT_TF
                )
                seg_sampled_ix = tf.gather(seg_ix, correspondence_ix, axis=0)

                # Create masks over sampled segments
                seg_feats_mask = tf.range(tf.shape(utt_mask)[1])[None, ...]
                batch_sampled_ix = tf.gather(batch_ix, correspondence_ix, axis=0)
                start_sampled_ix = tf.gather(start_ix, correspondence_ix, axis=0)
                end_sampled_ix = tf.gather(end_ix, correspondence_ix, axis=0)
                seg_feats_mask = tf.cast(
                    tf.logical_and(
                        seg_feats_mask >= start_sampled_ix,
                        seg_feats_mask < end_sampled_ix
                    ),
                    dtype=self.FLOAT_TF
                )

                # Collect IDs of speakers of utterances in which segments were sampled
                speaker_ids = tf.gather(self.speaker, batch_ix[1:])

                # Collect hidden states of sampled segments
                h = tf.gather_nd(
                    self.encoder_features[l],
                    seg_sampled_ix
                )

                # Collect acoustics of utterances in which segments were sampled
                seg_feats = tf.gather(self.X[:, :, :self.n_coef], batch_sampled_ix)
                seg_feats_src = seg_feats

                if self.correspondence_live_targets:
                    # Fourier-resample the acoustics to a fixed dimension, or mask with segment mask
                    if self.resample_correspondence:
                        seg_feats_T = tf.transpose(seg_feats, [0, 2, 1])
                        seg_feats_T = tf.unstack(seg_feats_T, num=self.n_correspondence)
                        new_seg_feats = []
                        for j, s in enumerate(seg_feats_T):
                            s = tf.boolean_mask(s, seg_feats_mask[j], axis=1)
                            s_resamp = self._resample_signal(
                                s,
                                self.resample_correspondence
                            )
                            new_seg_feats.append(s_resamp)
                        seg_feats = tf.stack(new_seg_feats, axis=0)
                        seg_feats = tf.transpose(seg_feats, [0, 2, 1])
                    else:
                        seg_feats *= seg_feats_mask

                return seg_feats_src, seg_feats, seg_feats_mask, h, speaker_ids

    def _compute_correspondence_ae_loss(
            self,
            implementation=3,
            n_timesteps=None,
            all_timesteps=False,
            alpha=1
    ):
        if self.data_normalization == 'range' and self.constrain_output:
            distance_func = 'binary_xent'
        elif self.data_type.lower() == 'text':
            distance_func = 'softmax_xent'
        else:
            distance_func = 'l2norm'

        with self.sess.as_default():
            with self.sess.graph.as_default():
                correspondence_ae_losses = []

                for l in range(self.layers_encoder - 1):
                    def create_correspondence_ae_loss_fn(implementation=3, n_timesteps=None, alpha=1):
                        def compute_loss_cur():
                            if self.correspondence_live_targets:
                                embeddings = self.correspondence_hidden_states[l]
                                targets = self.correspondence_feats[l]
                            else:
                                embeddings = self.correspondence_hidden_state_placeholders[l]
                                targets = self.correspondence_feature_placeholders[l]

                            # targets = tf.expand_dims(tf.expand_dims(spans, axis=0), axis=0)

                            if all_timesteps:
                                embeddings_src = self.encoder_features[l]
                                b = tf.shape(embeddings_src)[0]
                                t = tf.shape(embeddings_src)[1]
                                f = embeddings_src.shape[2]
                                embeddings_src = tf.reshape(embeddings_src, [b * t, f])
                                mask = tf.cast(tf.reshape(self.X_mask, [b * t]), dtype=tf.bool)
                                embeddings_src = tf.boolean_mask(embeddings_src, mask)
                                segs = self.segmentation_probs[l]
                                segs = tf.reshape(segs, [b * t])
                                segs = tf.boolean_mask(segs, mask)
                            else:
                                embeddings_src = embeddings

                            preds, _ = self._initialize_decoder(
                                embeddings_src,
                                self.resample_correspondence,
                                decoder_hidden_state_expansion_type=self.decoder_hidden_state_expansion_type,
                                decoder_positional_encoding_type=self.decoder_positional_encoding_type,
                                decoder_positional_encoding_as_mask=self.decoder_positional_encoding_as_mask,
                                decoder_positional_encoding_units=self.decoder_positional_encoding_units,
                                decoder_positional_encoding_transform=self.decoder_positional_encoding_transform,
                                decoder_positional_encoding_activation=self.decoder_positional_encoding_activation,
                                name='correspondence_%d' % l
                            )

                            embeddings_targ = tf.transpose(embeddings, [1,0])
                            
                            cos_sim = tf.tensordot(
                                tf.nn.l2_normalize(embeddings_src, axis=1),
                                tf.nn.l2_normalize(embeddings_targ, axis=0),
                                axes=1
                            )
                            weights = cos_sim

                            # if self.correspondence_live_targets:
                            #     targets = tf.expand_dims(tf.expand_dims(self.correspondence_feats[l], axis=0), axis=0)
                            # else:
                            #     targets = tf.expand_dims(tf.expand_dims(self.correspondence_feature_placeholders[l], axis=0), axis=0)
                            # preds = correspondence_autoencoder
                            #
                            # embeddings_by_timestep = tf.nn.l2_normalize(encoder_hidden_states, axis=-1)
                            # if self.correspondence_live_targets:
                            #     embeddings_by_segment = tf.nn.l2_normalize(self.correspondence_hidden_states[l], axis=-1)
                            # else:
                            #     embeddings_by_segment = tf.nn.l2_normalize(self.correspondence_hidden_state_placeholders[l], axis=-1)
                            # cos_sim = tf.tensordot(embeddings_by_timestep, tf.transpose(embeddings_by_segment, perm=[1, 0]), axes=1)
                            # weights = cos_sim

                            # Weighted average single target with weights from cos_sim
                            if implementation == 1:
                                weights = tf.nn.softmax(weights * alpha, axis=-1)
                                weights = tf.expand_dims(tf.expand_dims(weights, -1), -1)
                                targets = tf.reduce_sum(targets[None, None, ...] * weights, axis=0, keep_dims=True)

                                loss_cur = self._get_loss(
                                    targets,
                                    preds,
                                    use_dtw=self.use_dtw,
                                    distance_func=distance_func,
                                    reduce=False
                                )

                            # 1-best cos_sim single target
                            elif implementation == 2:
                                ix = tf.argmax(weights, axis=-1)
                                targets = tf.gather(targets, ix)

                                loss_cur = self._get_loss(
                                    targets,
                                    preds,
                                    use_dtw=self.use_dtw,
                                    distance_func=distance_func,
                                    reduce=False
                                )

                                loss_cur = tf.reduce_sum(loss_cur, axis=2)

                            # Multi-target weighted loss with weights from cos_sim
                            elif implementation == 3:
                                # Convert to cosine similarities tp unconstrained space,
                                # squashing to avoid atanh boundary violations

                                if not self.encoder_state_discretizer:
                                    weights = tf.atanh(weights * (1-self.epsilon))
                                    weights = tf.nn.softmax(weights * alpha)

                                targets = targets[None, ...]
                                preds = tf.expand_dims(preds, axis=-3)
                                loss_cur = self._get_loss(
                                    targets,
                                    preds,
                                    use_dtw=self.use_dtw,
                                    distance_func=distance_func,
                                    reduce=False
                                )

                                loss_cur = tf.reduce_sum(loss_cur * weights, axis=-1)
                                
                            else:
                                raise ValueError('Unrecognized correspondence AE loss implementation: %s' %implementation)

                            if all_timesteps:
                                loss_cur *= segs
                                loss_cur = tf.reduce_sum(loss_cur, axis=-1) / (tf.reduce_sum(segs, axis=-1) + self.epsilon)
                            else:
                                loss_cur = tf.reduce_mean(loss_cur, axis=-1)

                            loss_cur *= self.correspondence_loss_scale

                            return loss_cur

                        return compute_loss_cur

                    correspondence_ae_loss_cur = create_correspondence_ae_loss_fn(
                        implementation=implementation,
                        n_timesteps=n_timesteps,
                        alpha=alpha
                    )

                    loss_cur = tf.cond(self.step + 1 >= self.correspondence_start_step, correspondence_ae_loss_cur, lambda: 0.)
                    correspondence_ae_losses.append(loss_cur)

                return correspondence_ae_losses

    def _get_loss(
            self,
            targets,
            preds,
            use_dtw=False,
            distance_func='l2norm',
            weights=None,
            reduce=True,
            name=None
    ):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if use_dtw:
                    assert distance_func.lower() in ['norm', 'l2norm', 'mse'], 'Only l2norm distance is currently supported for DTW'
                    if weights is not None:
                        n_cells_weights = tf.reduce_prod(tf.shape(weights))
                        n_cells_losses = tf.reduce_prod(tf.shape(preds))
                        target_len = len(preds.shape) - 1
                        scaling_factor = tf.cast(n_cells_losses / n_cells_weights, dtype=self.FLOAT_TF)
                        while len(weights.shape) < target_len:
                            weights = weights[..., None]
                        while len(weights.shape) > target_len:
                            weights = tf.squeeze(weights, axis=-1)

                    loss = self._soft_dtw(
                        targets,
                        preds,
                        self.dtw_gamma,
                        mask=weights,
                        # weights_targets=weights[0],
                        # weights_preds=weights[1],
                        distance_func=distance_func
                    )

                    if reduce:
                        if weights is None:
                            loss = tf.reduce_mean(loss)
                        else:
                            loss = tf.reduce_sum(loss) / tf.maximum(tf.reduce_sum(weights) * scaling_factor, self.epsilon)
                else:
                    if weights is not None:
                        n_cells_weights = tf.reduce_prod(tf.shape(weights))
                        if distance_func.lower() in ['softmax_xent', 'cosine', 'arc']:
                            n_cells_losses = tf.reduce_prod(tf.shape(preds)[:-1])
                            target_len = len(preds.shape) - 1
                        else:
                            n_cells_losses = tf.reduce_prod(tf.shape(preds))
                            target_len = len(preds.shape)
                        scaling_factor = tf.cast(n_cells_losses / n_cells_weights, dtype=self.FLOAT_TF)
                        while len(weights.shape) < target_len:
                            weights = weights[..., None]

                    if distance_func.lower() == 'binary_xent':
                        loss = tf.nn.sigmoid_cross_entropy_with_logits(
                            labels=targets,
                            logits=preds
                        )
                    elif distance_func.lower() == 'softmax_xent':
                        loss = tf.nn.softmax_cross_entropy_with_logits_v2(
                            labels=targets,
                            logits=preds
                        )
                    elif distance_func.lower() in ['mse', 'l2norm']:
                        loss = (targets - preds) ** 2
                    elif distance_func.lower() == 'cosine':
                        loss = 1 - tf.reduce_sum(targets * preds, axis=-1)
                    elif distance_func.lower() == 'arc':
                        loss = tf.acos(tf.reduce_sum(targets * preds, axis=-1))
                    else:
                        raise ValueError('Unrecognized value for distance_func: %s' % distance_func)

                    if weights is not None:
                        loss *= weights

                    if reduce:
                        if weights is None:
                            loss = tf.reduce_mean(loss)
                        else:
                            loss = tf.reduce_sum(loss) / tf.maximum(tf.reduce_sum(weights) * scaling_factor, self.epsilon)

                # loss = tf.Print(loss, [tf.gradients(loss, weights)])

                return loss

    # Thanks to Ralph Mao (https://github.com/RalphMao) for this workaround
    def _restore_inner(self, path, predict=False, allow_missing=False):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                try:
                    if predict:
                        self.ema_saver.restore(self.sess, path)
                    else:
                        self.saver.restore(self.sess, path)
                except tf.errors.DataLossError:
                    stderr('Read failure during load. Trying from backup...\n')
                    if predict:
                        self.ema_saver.restore(self.sess, path[:-5] + '_backup.ckpt')
                    else:
                        self.saver.restore(self.sess, path[:-5] + '_backup.ckpt')
                except tf.errors.NotFoundError as err:  # Model contains variables that are missing in checkpoint, special handling needed
                    if allow_missing:
                        reader = tf.train.NewCheckpointReader(path)
                        saved_shapes = reader.get_variable_to_shape_map()
                        model_var_names = sorted(
                            [(var.name, var.name.split(':')[0]) for var in tf.global_variables()])
                        ckpt_var_names = sorted([(var.name, var.name.split(':')[0]) for var in tf.global_variables()
                                                 if var.name.split(':')[0] in saved_shapes])

                        model_var_names_set = set([x[1] for x in model_var_names])
                        ckpt_var_names_set = set([x[1] for x in ckpt_var_names])

                        missing_in_ckpt = model_var_names_set - ckpt_var_names_set
                        if len(missing_in_ckpt) > 0:
                            stderr(
                                'Checkpoint file lacked the variables below. They will be left at their initializations.\n%s.\n\n' % (
                                    sorted(list(missing_in_ckpt))))
                        missing_in_model = ckpt_var_names_set - model_var_names_set
                        if len(missing_in_model) > 0:
                            stderr(
                                'Checkpoint file contained the variables below which do not exist in the current model. They will be ignored.\n%s.\n\n' % (
                                    sorted(list(missing_in_ckpt))))

                        restore_vars = []
                        name2var = dict(
                            zip(map(lambda x: x.name.split(':')[0], tf.global_variables()), tf.global_variables()))

                        with tf.variable_scope('', reuse=True):
                            for var_name, saved_var_name in ckpt_var_names:
                                curr_var = name2var[saved_var_name]
                                var_shape = curr_var.get_shape().as_list()
                                if var_shape == saved_shapes[saved_var_name]:
                                    restore_vars.append(curr_var)

                        if predict:
                            self.ema_map = {}
                            for v in restore_vars:
                                self.ema_map[self.ema.average_name(v)] = v
                            saver_tmp = tf.train.Saver(self.ema_map)
                        else:
                            saver_tmp = tf.train.Saver(restore_vars)

                        saver_tmp.restore(self.sess, path)
                    else:
                        raise err








    ############################################################
    # Public methods
    ############################################################

    def n_minibatch(self, n):
        return math.ceil(float(n) / self.minibatch_size)

    def minibatch_scale(self, n):
        return float(n) / self.minibatch_size

    def check_numerics(self):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                for op in self.check_numerics_ops:
                    self.sess.run(op)

    def run_train_step(self, feed_dict, return_losses=True, return_reconstructions=False, return_labels=False):
        return NotImplementedError

    def classify_utterances(
            self,
            data=None,
            segtype=None,
            ix2label=None,
            plot=True,
            verbose=True
    ):
        eval_dict = {}
        binary = self.binary_classifier
        if segtype is None:
            segtype = self.segtype

        if self.task == 'classifier':
            segments = data.segments(segment_type=segtype)
            segments.reset_index(inplace=True)

            classifier_scores, labels_pred, encoding, summary = self.evaluate_classifier(
                data,
                segtype=segtype,
                ix2label=ix2label,
                plot=plot,
                verbose=verbose
            )
            eval_dict.update(classifier_scores)

            if binary:
                out_data = pd.DataFrame(encoding, columns=['d%d' % (i+1) for i in range(self.units_encoder[-1])])
                out_data = pd.concat([segments, out_data], axis=1)
            else:
                out_data = None
        else:
            if verbose:
                stderr('The system is in segmentation mode and does not perform utterance classification. Skipping classifier evaluation...\n')
            out_data = None
            summary = None

        return out_data, eval_dict, summary

    def evaluate_classifier(
            self,
            data,
            segtype=None,
            ix2label=None,
            plot=True,
            verbose=True
    ):
        summary = ''
        eval_dict = {}
        binary = self.binary_classifier
        if segtype is None:
            segtype = self.segtype

        if self.task == 'classifier':
            if verbose:
                stderr('Evaluating utterance classifier...\n')

            n = data.get_n(self.val_data_name)
            if self.pad_seqs:
                if not np.isfinite(self.eval_minibatch_size):
                    minibatch_size = n
                else:
                    minibatch_size = self.eval_minibatch_size
            else:
                minibatch_size = 1
            n_minibatch = data.get_n_minibatch(self.val_data_name, minibatch_size)

            data_feed = data.get_data_feed(self.val_data_name, minibatch_size=minibatch_size, randomize=False)
            labels = data.labels(one_hot=False, segment_type=segtype)

            with self.sess.as_default():
                with self.sess.graph.as_default():
                    self.set_predict_mode(True)

                    if verbose:
                        stderr('  Predicting labels...\n\n')
                        pb = tf.contrib.keras.utils.Progbar(n_minibatch)

                    to_run = []

                    labels_pred = []
                    to_run.append(self.labels_post)

                    if binary:
                        encoding = []
                        encoding_entropy = []
                        to_run += [self.encoding_post, self.encoding_entropy]
                    else:
                        encoding = None

                    for i, batch in enumerate(data_feed):
                        X_batch = batch['X']
                        X_mask_batch = batch['X_mask']
                        y_bwd_batch = batch['y']
                        y_bwd_mask_batch = batch['y_mask']
                        speaker_batch = batch['speaker']

                        fd_minibatch = {
                            self.X: X_batch,
                            self.X_mask: X_mask_batch,
                            self.y_bwd: y_bwd_batch,
                            self.y_bwd_mask: y_bwd_mask_batch,
                            self.training: False,
                            self.speaker: speaker_batch
                        }

                        out = self.sess.run(
                            to_run,
                            feed_dict=fd_minibatch
                        )

                        labels_pred_batch = out[0]
                        labels_pred.append(labels_pred_batch)

                        if binary:
                            encoding_batch, encoding_entropy_batch = out[1:]
                            encoding.append(encoding_batch)
                            encoding_entropy.append(encoding_entropy_batch)

                        if verbose:
                            pb.update(i + 1, values=[])

                    if verbose:
                        stderr('\n')

                    labels_pred = np.concatenate(labels_pred, axis=0)

                    if binary:
                        encoding = np.concatenate(encoding, axis=0)
                        encoding_entropy = np.concatenate(encoding_entropy, axis=0).mean()

                    if not self.units_encoder[-1]:
                        k = 2 ** self.emb_dim
                    else:
                        k = 2 ** self.units_encoder[-1]

                    eval_dict, summary = self._evaluate_classifier_inner(
                        labels,
                        labels_pred,
                        k=k,
                        binary_encoding=encoding,
                        encoding_entropy=encoding_entropy,
                        plot=plot,
                        ix2label=ix2label,
                        verbose=verbose
                    )

        else:
            if verbose:
                stderr('The system is in segmentation mode and does not perform utterance classification. Skipping classifier evaluation...\n')
            labels_pred = None
            encoding = None

        return eval_dict, labels_pred, encoding, summary

    def _evaluate_classifier_inner(
            self,
            labels_true,
            labels_pred,
            k=None,
            binary_encoding=None,
            encoding_entropy=None,
            plot=True,
            suffix=None,
            ix2label=None,
            random_baseline=True,
            frequency_weighted_baseline=False,
            verbose=True
    ):
        summary = ''
        eval_dict = {'system': {}}

        if plot:
            if self.task == 'classifier' and ix2label is not None:
                labels_string = np.vectorize(lambda x: ix2label[x])(labels_true.astype('int'))

                plot_label_heatmap(
                    labels_string,
                    labels_pred.astype('int'),
                    label_map=self.label_map,
                    dir=self.outdir
                )

                if binary_encoding is not None:
                    if self.keep_plot_history:
                        iter = self.step.eval(session=self.sess) + 1
                        suffix = '_%d.png' % iter
                    else:
                        suffix = '.png'

                    plot_binary_unit_heatmap(
                        labels_string,
                        binary_encoding,
                        label_map=self.label_map,
                        dir=self.outdir,
                        suffix=suffix
                    )

        h, c, v = homogeneity_completeness_v_measure(labels_true, labels_pred)
        fmi = fowlkes_mallows_score(labels_true, labels_pred)

        eval_dict['system']['homogeneity'] = h
        eval_dict['system']['completeness'] = c
        eval_dict['system']['v_measure'] = v
        eval_dict['system']['fmi'] = fmi

        pred_eval_summary = ''
        if encoding_entropy is not None:
            pred_eval_summary += '  Encoding entropy: %s\n\n' % encoding_entropy
        pred_eval_summary += '  Labeling scores using %d latent categories (system):\n' % len(np.unique(labels_pred))
        pred_eval_summary += '    Homogeneity:                 %s\n' % h
        pred_eval_summary += '    Completeness:                %s\n' % c
        pred_eval_summary += '    V-measure:                   %s\n' % v
        pred_eval_summary += '    Fowlkes-Mallows index:       %s\n\n' % fmi

        if verbose:
            stderr(pred_eval_summary)
            sys.stderr.flush()

        summary += pred_eval_summary

        if self.binary_classifier and random_baseline:
            if 'random' not in eval_dict:
                eval_dict['random'] = {}
            if k is None:
                k = len(np.unique(labels_pred))
            if frequency_weighted_baseline:
                labels_rand = np.random.permutation(labels_pred)
                # category_ids, category_counts = np.unique(labels_pred, return_counts=True)
                # category_probs = category_counts / category_counts.sum()
                # labels_rand = np.argmax(np.random.multinomial(1, category_probs, size=labels_pred.shape), axis=-1)
            else:
                labels_rand = np.random.randint(0, k, labels_pred.shape)

            h, c, v = homogeneity_completeness_v_measure(labels_true, labels_rand)
            fmi = fowlkes_mallows_score(labels_true, labels_rand)

            eval_dict['random']['homogeneity'] = h
            eval_dict['random']['completeness'] = c
            eval_dict['random']['v_measure'] = v
            eval_dict['random']['fmi'] = fmi

            rand_eval_summary = ''
            rand_eval_summary += '  Labeling scores using %d latent categories (random):\n' % k
            rand_eval_summary += '    Homogeneity:                 %s\n' % h
            rand_eval_summary += '    Completeness:                %s\n' % c
            rand_eval_summary += '    V-measure:                   %s\n' % v
            rand_eval_summary += '    Fowlkes-Mallows index:       %s\n\n' % fmi

            if verbose:
                stderr(rand_eval_summary)
                sys.stderr.flush()

            summary += rand_eval_summary

        return eval_dict, summary

    def score_acoustics(self, data, segment_tables):
        segmentation_scores = []
        summary = ''

        if self.data_type.lower() == 'acoustic':
            for i, segment_table in enumerate(segment_tables):
                summary += '  Layer %s\n' % (i + 1)
                summary += '    Num segments: %d\n' % len(segment_table)
                summary += '    Mean segment length: %.4fs\n\n' % (segment_table.end - segment_table.start).mean()

                s = data.score_segmentation('phn', segment_table, tol=0.02)[0]
                B_P, B_R, B_F = f_measure(s['b_tp'], s['b_fp'], s['b_fn'])
                W_P, W_R, W_F = f_measure(s['w_tp'], s['w_fp'], s['w_fn'])
                summary += '    Phonemes:\n'
                summary += '       B P: %s\n' % B_P
                summary += '       B R: %s\n' % B_R
                summary += '       B F: %s\n\n' % B_F
                summary += '       W P: %s\n' % W_P
                summary += '       W R: %s\n' % W_R
                summary += '       W F: %s\n\n' % W_F

                segmentation_scores.append({
                    'phn': {
                    'b_p': B_P,
                    'b_r': B_R,
                    'b_f': B_F,
                    'w_p': W_P,
                    'w_r': W_R,
                    'w_f': W_F,
                    }
                })

                s = data.score_segmentation('wrd', segment_table, tol=0.03)[0]
                B_P, B_R, B_F = f_measure(s['b_tp'], s['b_fp'], s['b_fn'])
                W_P, W_R, W_F = f_measure(s['w_tp'], s['w_fp'], s['w_fn'])
                summary += '    Words:\n'
                summary += '       B P: %s\n' % B_P
                summary += '       B R: %s\n' % B_R
                summary += '       B F: %s\n\n' % B_F
                summary += '       W P: %s\n' % W_P
                summary += '       W R: %s\n' % W_R
                summary += '       W F: %s\n\n' % W_F

                segmentation_scores[-1]['wrd'] = {
                    'b_p': B_P,
                    'b_r': B_R,
                    'b_f': B_F,
                    'w_p': W_P,
                    'w_r': W_R,
                    'w_f': W_F,
                }

            data.dump_segmentations_to_textgrid(
                outdir=self.outdir,
                suffix='',
                segments=segment_tables + ['phn', 'wrd']
            )

        else:
            stderr('Cannot score acoustics for text data. Skipping...\n')

        return segmentation_scores, summary

    def score_text(self, data, segment_tables):
        segmentation_scores = []
        summary = ''
        if self.data_type.lower() == 'text':
            for i, segment_table in enumerate(segment_tables):
                summary += '  Layer %s\n' % (i + 1)
                summary += '    Num segments: %d\n' % len(segment_tables)
                summary += '    Mean segment length: %.4f characters\n\n' % (segment_table.end - segment_table.start).mean()

                s = data.score_text_segmentation('wrd', segment_table)[0]
                B_P, B_R, B_F = f_measure(s['b_tp'], s['b_fp'], s['b_fn'])
                W_P, W_R, W_F = f_measure(s['w_tp'], s['w_fp'], s['w_fn'])
                L_P, L_R, L_F = f_measure(s['l_tp'], s['l_fp'], s['l_fn'])
                summary += '    Words:\n'
                summary += '       B P: %s\n' % B_P
                summary += '       B R: %s\n' % B_R
                summary += '       B F: %s\n\n' % B_F
                summary += '       W P: %s\n' % W_P
                summary += '       W R: %s\n' % W_R
                summary += '       W F: %s\n\n' % W_F
                summary += '       L P: %s\n' % L_P
                summary += '       L R: %s\n' % L_R
                summary += '       L F: %s\n\n' % L_F

                segmentation_scores.append({
                    'wrd': {
                        'b_p': B_P,
                        'b_r': B_R,
                        'b_f': B_F,
                        'w_p': W_P,
                        'w_r': W_R,
                        'w_f': W_F,
                        'l_p': L_P,
                        'l_r': L_R,
                        'l_f': L_F,
                    }
                })

                data.dump_segmentations_to_textfile(
                    outdir=self.outdir,
                    suffix='_l%d' % (i + 1),
                    segments=[segment_table],
                    parent_segments='vad'
                )
        else:
            stderr('Cannot score text for acoustic data. Skipping...\n')

        return segmentation_scores, summary

    def evaluate_segmenter(
            self,
            data,
            whole_file=True,
            segtype=None,
            random_baseline=True,
            plot=True,
            save_embeddings=True,
            ix2label=None,
            verbose=True
    ):
        report_classeval = False

        if 'hmlstm' in self.encoder_type.lower():
            summary = ''

            if verbose:
                stderr('Evaluating segmenter...\n')

            if segtype is None:
                segtype = self.segtype

            if whole_file:
                minibatch_size = 1
                n_minibatch = data.get_n(self.val_data_name)
            else:
                n = data.get_n(self.val_data_name)
                if self.pad_seqs:
                    if not np.isfinite(self.eval_minibatch_size):
                        minibatch_size = n
                    else:
                        minibatch_size = self.eval_minibatch_size
                else:
                    minibatch_size = 1
                n_minibatch = data.get_n_minibatch(self.val_data_name, minibatch_size)

            data_feed = data.get_data_feed(self.val_data_name, minibatch_size=minibatch_size, randomize=False)
            n_layers = len(self.segmentation_probs)

            with self.sess.as_default():
                with self.sess.graph.as_default():
                    self.set_predict_mode(True)

                    if verbose:
                        stderr('Extracting segmenter states...\n')
                        pb = tf.contrib.keras.utils.Progbar(n_minibatch)

                    if self.streaming or whole_file:
                        padding = None

                        # Lists are ragged shape [N_LAYERS, N_FILES, N_TIMESTEPS]
                        segmentations = [[] for _ in range(n_layers)]
                        states = [[] for _ in range(n_layers)]
                        phn_boundaries = []
                        phn_labels = []
                        wrd_boundaries = []
                        wrd_labels = []

                        for i, file in enumerate(data_feed):
                            X_batch = file['X']
                            fixed_boundaries_batch = file['fixed_boundaries']
                            phn_boundaries_batch = np.squeeze(file['phn_boundaries'])
                            phn_boundaries.append(phn_boundaries_batch)
                            phn_labels_batch = np.squeeze(file['phn_labels'])
                            phn_labels.append(phn_labels_batch)
                            wrd_boundaries_batch = np.squeeze(file['wrd_boundaries'])
                            wrd_boundaries.append(wrd_boundaries_batch)
                            wrd_labels_batch = np.squeeze(file['wrd_labels'])
                            wrd_labels.append(wrd_labels_batch)
                            if self.oracle_boundaries:
                                oracle_boundaries_batch = file[self.oracle_boundaries + '_boundaries']
                            speaker_batch = file['speaker']

                            fd_minibatch = {
                                self.X: X_batch,
                                self.fixed_boundaries_placeholder: fixed_boundaries_batch,
                                self.training: False,
                                self.speaker: speaker_batch
                            }

                            if self.oracle_boundaries:
                                fd_minibatch[self.oracle_boundaries_placeholder] = oracle_boundaries_batch

                            if fixed_boundaries_batch is not None:
                                fd_minibatch[self.fixed_boundaries_placeholder] = fixed_boundaries_batch

                            segmentations_cur, states_cur = self.sess.run(
                                [
                                    self.segmentations,
                                    self.encoder_feature_targets
                                ],
                                feed_dict=fd_minibatch
                            )

                            for l in range(n_layers):
                                states[l].append(np.squeeze(states_cur[l]))
                                segmentations[l].append(np.squeeze(segmentations_cur[l]))

                            if verbose:
                                pb.update(i+1, values=[])

                    else:
                        padding = self.input_padding
                        segmentations = [[] for _ in range(n_layers)]
                        states = [[] for _ in range(n_layers)]

                        for i, batch in enumerate(data_feed):
                            X_batch = batch['X']
                            X_mask_batch = batch['X_mask']
                            speaker_batch = batch['speaker']
                            fixed_boundaries_batch = batch['fixed_boundaries']

                            fd_minibatch = {
                                self.X: X_batch,
                                self.training: False,
                                self.speaker: speaker_batch
                            }

                            if not self.streaming:
                                fd_minibatch[self.X_mask] = X_mask_batch

                            if fixed_boundaries_batch is not None:
                                fd_minibatch[self.fixed_boundaries_placeholder] = fixed_boundaries_batch

                            [segmentations_cur, states_cur] = self.sess.run(
                                [
                                    self.segmentations,
                                    self.encoder_features
                                ],
                                feed_dict=fd_minibatch
                            )

                            X_mask.append(X_mask_batch)
                            for l in range(n_layers):
                                segmentations[l].append(segmentations_cur[l])
                                states[l].append(states_cur[l])

                            if verbose:
                                pb.update(i+1, values=[])

                        new_segmentations = []
                        new_states = []

                        for l in range(n_layers):
                            new_segmentations.append(
                                np.concatenate(segmentations[l], axis=0)
                            )
                            new_states.append(
                                np.concatenate(states[l], axis=0)
                            )

                        segmentations = new_segmentations
                        states = new_states

                    if verbose:
                        stderr('Computing segment tables...\n')

                    smoothing_algorithm = None
                    n_points = None

                    if self.xent_state_predictions:
                        state_activation = 'sigmoid'
                    else:
                        state_activation = self.encoder_inner_activation

                    pred_tables = data.get_segment_tables(
                        segmentations=segmentations,
                        parent_segment_type=segtype,
                        states=states,
                        phn_labels=phn_labels,
                        wrd_labels=wrd_labels,
                        state_activation=state_activation,
                        smoothing_algorithm=smoothing_algorithm,
                        smoothing_algorithm_params=None,
                        n_points=n_points,
                        padding=padding
                    )

                    if self.data_type.lower() == 'acoustic':
                        phn_tables = data.get_segment_tables(
                            segmentations=[phn_boundaries] * (self.layers_encoder - 1),
                            states=states,
                            phn_labels=phn_labels,
                            wrd_labels=None,
                            parent_segment_type=segtype,
                            state_activation=state_activation,
                            smoothing_algorithm=smoothing_algorithm,
                            smoothing_algorithm_params=None,
                            n_points=n_points,
                            padding=padding
                        )

                        if self.feature_map is not None:
                            phn_tables = [pd.merge(x, self.feature_map, left_on=['phn_label'], right_on=['symbol']) for x in phn_tables]
                        if self.label_map is not None:
                            for table in phn_tables:
                                ipa = pd.Series(table.phn_label).replace(self.label_map)
                                table['IPA'] = ipa
                        phn_tables = [x.sort_values(['speaker', 'fileID', 'start']) for x in phn_tables]
                    else:
                        phn_tables = None

                    wrd_tables = data.get_segment_tables(
                        segmentations=[wrd_boundaries] * (self.layers_encoder - 1),
                        states=states,
                        phn_labels=None,
                        wrd_labels=wrd_labels,
                        parent_segment_type=segtype,
                        state_activation=state_activation,
                        smoothing_algorithm=smoothing_algorithm,
                        smoothing_algorithm_params=None,
                        n_points=n_points,
                        padding=padding
                    )

                    if verbose:
                        stderr('Evaluating segmentation...\n')

                    summary += '\nSEGMENTATION EVAL:\n\n'

                    if self.data_type.lower() == 'acoustic':
                        scores_cur, summary_cur = self.score_acoustics(data, pred_tables)
                    else:
                        scores_cur, summary_cur = self.score_text(data, pred_tables)

                    scores = {
                        'segmentation_scores': scores_cur
                    }
                    summary += summary_cur

                    if verbose:
                        stderr('Evaluating classification...\n')

                    if report_classeval:
                        summary += '\nCLASSIFICATION EVAL:\n\n'
                    scores['classification_scores'] = []
                    if self.data_type.lower() == 'acoustic':
                        segtypes = ['phn', 'wrd']
                    else:
                        segtypes = ['wrd']
                    for l in range(len(pred_tables)):
                        scores['classification_scores'].append({})
                        for s in segtypes:
                            if report_classeval:
                                summary += 'LAYER %d, GOLD=%s\n' % (l + 1, s)

                            scores['classification_scores'][l][s] = {'goldseg': {}, 'predseg': {}}

                            if s == 'phn':
                                seg_table = phn_tables[l]
                            else:
                                seg_table = wrd_tables[l]

                            if report_classeval:
                                summary += '  Using gold segmentations\n'
                            goldseg_score, summary_cur = self._evaluate_classifier_inner(
                                seg_table[s + '_label'],
                                seg_table.label,
                                plot=plot,
                                random_baseline=random_baseline,
                                ix2label=None,
                                verbose=False
                            )
                            scores['classification_scores'][l][s]['goldseg'] = goldseg_score
                            if report_classeval:
                                summary += summary_cur

                            if report_classeval:
                                summary += '  Using predicted segmentations\n'
                            predseg_score, summary_cur = self._evaluate_classifier_inner(
                                pred_tables[l][s + '_label'],
                                pred_tables[l].label,
                                plot=plot,
                                random_baseline=random_baseline,
                                ix2label=None,
                                verbose=False
                            )
                            scores['classification_scores'][l][s]['predseg'] = predseg_score
                            if report_classeval:
                                summary += summary_cur

                    if save_embeddings:
                        if verbose:
                            stderr('Saving segment tables...\n')
                        for l in range(self.layers_encoder - 1):
                            pred_tables[l].to_csv(
                                self.outdir + '/embeddings_pred_segs_l%d.csv' % l,
                                sep=' ',
                                index=False
                            )
                            wrd_tables[l].to_csv(
                                self.outdir + '/embeddings_gold_wrd_segs_l%d.csv' % l,
                                sep=' ',
                                index=False
                            )
                            matched_wrd = data.extract_matching_segment_embeddings(
                                'wrd',
                                pred_tables[l],
                                tol=0.02
                            )
                            matched_wrd.to_csv(
                                self.outdir + '/embeddings_matched_wrd_segs_l%d.csv' % l,
                                sep=' ',
                                index=False
                            )

                            if self.data_type.lower() == 'acoustic':
                                phn_tables[l].to_csv(
                                    self.outdir + '/embeddings_gold_phn_segs_l%d.csv' % l, sep=' ',
                                    index=False
                                )
                                matched_phn = data.extract_matching_segment_embeddings(
                                    'phn',
                                    pred_tables[l],
                                    tol=0.02
                                )
                                matched_phn.rename(columns={'label': 'phn_label'}, inplace=True)
                                matched_phn.to_csv(
                                    self.outdir + '/embeddings_matched_phn_segs_l%d.csv' % l,
                                    sep=' ',
                                    index=False
                                )

                    stderr(summary)

                    with open(self.outdir + '/initial_classifier_eval.txt', 'w') as f:
                        f.write(summary)

                    self.set_predict_mode(False)

        else:
            if verbose:
                stderr('The system is in classification mode and does not perform utterance segmentation. Skipping segmenter evaluation...\n')
            scores = {}

        return scores, summary

    def run_evaluation(
            self,
            data,
            n_plot=10,
            ix2label=None,
            training=False,
            segtype=None,
            random_baseline=True,
            evaluate_classifier=True,
            evaluate_segmenter=True,
            save_embeddings=True,
            verbose=True
    ):

        if segtype is None:
            segtype = self.segtype

        if n_plot:
            self.plot_utterances(
                data,
                n_plot=n_plot,
                ix2label=ix2label,
                training=training,
                segtype=segtype,
                verbose=verbose
            )

        if self.task == 'classifier' and evaluate_classifier:
            eval_dict, labels_pred, encoding, _ = self.evaluate_classifier(
                data,
                segtype=segtype,
                ix2label=ix2label,
                plot=n_plot,
                verbose=verbose
            )

        elif self.task != 'classifier' and evaluate_segmenter:
            eval_dict, _ = self.evaluate_segmenter(
                data,
                segtype=segtype,
                plot=n_plot is not None,
                ix2label=ix2label,
                random_baseline=random_baseline,
                save_embeddings=save_embeddings,
                verbose=verbose
            )

        else:
            eval_dict = {}

        if verbose:
            stderr('\n')

        return eval_dict

    def static_correspondence_targets(self):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                return self.n_correspondence \
                       and (not self.correspondence_live_targets) \
                       and self.step.eval(session=self.sess) + 1 >= self.correspondence_start_step

    def collect_correspondence_targets(
            self,
            info_dict=None,
            data=None
    ):
        assert (info_dict is not None or data is not None), "Either **info_dict** or **data** must be provided."
        if info_dict is None:
            data_feed_train = data.get_data_feed(self.train_data_name, minibatch_size=self.minibatch_size, randomize=True)
            batch = next(data_feed_train)

            X_batch = batch['X']
            X_mask_batch = batch['X_mask']
            speaker_batch = batch['speaker']
            fixed_boundaries_batch = batch['fixed_boundaries']
            if self.oracle_boundaries:
                oracle_boundaries_batch = batch[self.oracle_boundaries + '_boundaries']

            to_run = []
            to_run_names = []
            [to_run.append(seg_states) for seg_states in self.correspondence_hidden_states]
            [to_run.append(seg_feats) for seg_feats in self.correspondence_feats]
            [to_run.append(seg_feats_mask) for seg_feats_mask in self.correspondence_mask]
            [to_run_names.append('correspondence_seg_states_l%d' % i) for i in range(len(self.correspondence_hidden_states))]
            [to_run_names.append('correspondence_seg_feats_l%d' % i) for i in range(len(self.correspondence_feats))]
            [to_run_names.append('correspondence_seg_feats_mask_l%d' % i) for i in range(len(self.correspondence_mask))]

            feed_dict = {
                self.X: X_batch,
                self.X_mask: X_mask_batch,
                self.speaker: speaker_batch
            }

            if fixed_boundaries_batch is not None:
                feed_dict[self.fixed_boundaries_placeholder] = fixed_boundaries_batch

            if self.oracle_boundaries:
                feed_dict[self.oracle_boundaries_placeholder] = oracle_boundaries_batch


            with self.sess.as_default():
                with self.sess.graph.as_default():
                    output = self.sess.run(to_run, feed_dict=feed_dict)

            info_dict = {}
            for i, x in enumerate(output):
                info_dict[to_run_names[i]] = x

        segment_embeddings = []
        segment_spans = []
        for j in range(len(self.correspondence_feats)):
            states_cur = info_dict['correspondence_seg_states_l%d' % j]
            segment_embeddings.append(states_cur)

            feats_cur = info_dict['correspondence_seg_feats_l%d' % j]
            feats_mask_cur = info_dict['correspondence_seg_feats_mask_l%d' % j]
            spans_cur = self.process_previous_segments(
                feats_cur,
                feats_mask_cur
            )
            segment_spans.append(spans_cur)

        return segment_embeddings, segment_spans

    def run_checkpoint(
            self,
            data,
            save=True,
            evaluate=True,
            save_embeddings=True,
            log=True,
            loss=None,
            reg_loss=None,
            reconstruction_loss=None,
            prediction_loss=None,
            correspondence_loss=None,
            encoder_lm_losses=None,
            encoder_speaker_adversarial_losses=None,
            encoder_passthru_adversarial_losses=None,
            random_baseline=True,
            ix2label=None,
            n_plot=10,
            check_numerics=False,
            verbose=True
    ):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                try:
                    if check_numerics:
                        self.check_numerics()

                    if verbose:
                        t0 = time.time()

                    if save:
                        if verbose:
                            stderr('Saving model...\n')

                        self.save()

                    if self.task == 'streaming_autoencoder':
                        eval_dict = {}
                    else:
                        eval_dict = self.run_evaluation(
                            data,
                            ix2label=ix2label,
                            segtype=self.segtype,
                            evaluate_classifier=evaluate,
                            evaluate_segmenter=evaluate,
                            save_embeddings=save_embeddings,
                            random_baseline=random_baseline,
                            n_plot=n_plot,
                            verbose=verbose
                        )

                    if log:
                        fd_summary = {}

                        if loss is not None:
                            fd_summary[self.loss_summary] = loss

                        if reg_loss is not None:
                            fd_summary[self.reg_summary] = reg_loss

                        if reconstruction_loss is not None:
                            fd_summary[self.loss_reconstruction_summary] = None

                        if prediction_loss is not None:
                            fd_summary[self.loss_prediction_summary] = prediction_loss

                        
                        if self.use_correspondence_loss:
                            for l in range(self.layers_encoder - 1):
                                if self.correspondence_loss_scale[l]:
                                    fd_summary[self.correspondence_loss_summary[l]] = correspondence_loss[l]

                        if encoder_lm_losses is not None:
                            for l in range(self.layers_encoder):
                                fd_summary[self.encoder_lm_loss_summary[l]] = encoder_lm_losses[l]

                        if encoder_speaker_adversarial_losses is not None:
                            for l in range(self.layers_encoder):
                                fd_summary[self.encoder_speaker_adversarial_loss_summary[l]] = encoder_speaker_adversarial_losses[l]

                        if encoder_passthru_adversarial_losses is not None:
                            for l in range(self.layers_encoder):
                                fd_summary[self.encoder_passthru_adversarial_loss_summary[l]] = encoder_passthru_adversarial_losses[l]

                        if len(fd_summary) > 0:
                            summary_objective = self.sess.run(self.summary_objective, feed_dict=fd_summary)
                            self.writer.add_summary(summary_objective, self.step.eval(session=self.sess))

                        fd_summary = {}

                        if 'classification_scores' in eval_dict:
                            classification_scores = eval_dict['classification_scores']
                            if self.task.lower() == 'classifier':
                                n_layers = 1
                            else:
                                n_layers = self.layers_encoder - 1
                            if self.task.lower() == 'classifier':
                                segtypes = [self.segtype]
                            elif self.data_type.lower() == 'acoustic':
                                segtypes = ['phn', 'wrd']
                            else:
                                segtypes = ['wrd']
                            for l in range(n_layers):
                                for s in segtypes:
                                    for g in ['goldseg', 'predseg']:
                                        for t in ['system', 'random']:
                                            src = classification_scores[l][s][g][t]
                                            tgt = self.classification_scores[l][s][g][t]
                                            fd_summary[tgt['homogeneity']] = src['homogeneity']
                                            fd_summary[tgt['completeness']] = src['completeness']
                                            fd_summary[tgt['v_measure']] = src['v_measure']
                                            fd_summary[tgt['fmi']] = src['fmi']

                            summary_classification = self.sess.run(self.summary_classification, feed_dict=fd_summary)
                            self.writer.add_summary(summary_classification, self.step.eval(session=self.sess))

                        if 'segmentation_scores' in eval_dict:
                            segmentation_scores = eval_dict['segmentation_scores']
                            for i in range(self.layers_encoder - 1):
                                if self.data_type.lower() == 'acoustic':
                                    for s in ['phn', 'wrd']:
                                        src = segmentation_scores[i][s]
                                        tgt = self.segmentation_scores[i][s]
                                        fd_summary[tgt['b_p']] = src['b_p']
                                        fd_summary[tgt['b_r']] = src['b_r']
                                        fd_summary[tgt['b_f']] = src['b_f']
                                        fd_summary[tgt['w_p']] = src['w_p']
                                        fd_summary[tgt['w_r']] = src['w_r']
                                        fd_summary[tgt['w_f']] = src['w_f']
                                else:
                                    src = segmentation_scores[i]['wrd']
                                    tgt = self.segmentation_scores[i]['wrd']
                                    fd_summary[tgt['b_p']] = src['b_p']
                                    fd_summary[tgt['b_r']] = src['b_r']
                                    fd_summary[tgt['b_f']] = src['b_f']
                                    fd_summary[tgt['w_p']] = src['w_p']
                                    fd_summary[tgt['w_r']] = src['w_r']
                                    fd_summary[tgt['w_f']] = src['w_f']
                                    fd_summary[tgt['l_p']] = src['l_p']
                                    fd_summary[tgt['l_r']] = src['l_r']
                                    fd_summary[tgt['l_f']] = src['l_f']

                            summary_segmentation = self.sess.run(self.summary_segmentation, feed_dict=fd_summary)
                            self.writer.add_summary(summary_segmentation, self.step.eval(session=self.sess))

                    if verbose:
                        t1 = time.time()
                        time_str = pretty_print_seconds(t1 - t0)
                        stderr('Checkpoint time: %s\n' % time_str)

                except tf.errors.InvalidArgumentError as e:
                    stderr(str(e) + '\n')
                    if verbose:
                        stderr('Numerics check failed. Aborting and reloading from previous checkpoint...\n')
                    self.load()

    def fit(
            self,
            train_data,
            val_data=None,
            n_iter=None,
            ix2label=None,
            n_plot=10,
            verbose=True
    ):
        if self.step.eval(session=self.sess) == 0:
            if verbose:
                stderr('Saving initial weights...\n')
            self.save()

        if verbose:
            usingGPU = tf.test.is_gpu_available()
            stderr('Using GPU: %s\n' % usingGPU)

        stderr('Extracting training and cross-validation data...\n')
        t0 = time.time()

        if val_data is None:
            val_data = train_data

        if self.task == 'streaming_autoencoder':
            N_FOLD = 512
            X, new_series = train_data.features(fold=N_FOLD, mask=None)
            n_train = len(X)

        else:
            cache_data(
                    train_data=train_data,
                    val_data=val_data,
                    streaming=self.streaming,
                    max_len=self.max_len,
                    window_len_bwd=self.window_len_bwd,
                    window_len_fwd=self.window_len_fwd,
                    segtype=self.segtype,
                    data_normalization=self.data_normalization,
                    reduction_axis=self.reduction_axis,
                    use_normalization_mask=self.use_normalization_mask,
                    predict_deltas=self.predict_deltas,
                    input_padding=self.input_padding,
                    target_padding=self.target_padding,
                    reverse_targets=self.reverse_targets,
                    resample_inputs=self.resample_inputs,
                    resample_targets_bwd=self.resample_targets_bwd,
                    resample_targets_fwd=self.resample_targets_fwd,
                    task=self.task,
                    data_type=self.data_type
            )

            n_train = train_data.get_n(self.train_data_name)

        t1 = time.time()

        stderr('Training and cross-validation data extracted in %ds\n\n' % (t1 - t0))
        sys.stderr.flush()

        if n_iter is None:
            n_iter = self.n_iter

        if verbose:
            stderr('*' * 100 + '\n')
            stderr(self.report_settings())
            stderr('\n')
            stderr(self.report_n_params())
            stderr('\n')
            stderr('*' * 100 + '\n\n')

        if self.n_correspondence and not self.correspondence_live_targets:
            segment_embeddings = None
            segment_spans = None

        with self.sess.as_default():
            with self.sess.graph.as_default():
                if self.pad_seqs:
                    if not np.isfinite(self.minibatch_size):
                        minibatch_size = n_train
                    else:
                        minibatch_size = self.minibatch_size
                    n_minibatch = math.ceil(float(n_train) / minibatch_size)
                else:
                    minibatch_size = 1
                    n_minibatch = n_train

                if self.streaming:
                    if self.eval_freq:
                        eval_freq = self.eval_freq
                    else:
                        eval_freq = np.inf
                    if self.save_freq:
                        save_freq = self.save_freq
                    else:
                        save_freq = np.inf
                    if self.log_freq:
                        log_freq = self.log_freq
                    else:
                        log_freq = np.inf

                    n_pb = min(n_minibatch, eval_freq, save_freq, log_freq)
                else:
                    n_pb = n_minibatch

                # if not self.initial_evaluation_complete.eval(session=self.sess):
                # if True:
                if False:
                    self.run_checkpoint(
                        val_data,
                        save=False,
                        log=self.log_freq > 0,
                        evaluate=self.eval_freq > 0,
                        n_plot=n_plot,
                        ix2label=ix2label,
                        save_embeddings=True,
                        check_numerics=False,
                        verbose=verbose
                    )
                    self.sess.run(self.set_initial_evaluation_complete)
                    self.save()

                while self.global_step.eval(session=self.sess) < n_iter:
                    self.set_update_mode(verbose=False)

                    if verbose:
                        t0_iter = time.time()
                        stderr('-' * 50 + '\n')
                        stderr('Iteration %d\n' % int(self.global_step.eval(session=self.sess) + 1))
                        stderr('\n')

                        if self.streaming:
                            if n_pb > 1:
                                stderr('Running minibatches %d-%d (out of %d)...\n' % (1, n_pb, n_minibatch))
                            else:
                                stderr('Running minibatch %d (out of %d)...\n' % (n_pb, n_minibatch))
                        else:
                            stderr('Running minibatch updates...\n')

                        stderr('Update mode: %s\n' % self.update_mode)

                        if self.n_pretrain_steps and (self.step.eval(session=self.sess) <= self.n_pretrain_steps):
                            stderr('Pretraining decoder...\n')

                        if self.optim_name is not None and self.lr_decay_family is not None:
                            stderr('Learning rate: %s\n' % self.lr.eval(session=self.sess))

                        if self.curriculum_t is not None:
                            if self.curriculum_type.lower() == 'hard':
                                stderr('Curriculum window length: %s\n' % self.curriculum_t.eval(session=self.sess))
                            elif self.curriculum_type.lower() == 'exp':
                                stderr('Curriculum decay rate: %s\n' % (1. / self.curriculum_t.eval(session=self.sess)))
                            else:
                                stderr('Curriculum window soft bound location: %s\n' % self.curriculum_t.eval(session=self.sess))

                        if self.boundary_slope_annealing_rate:
                            sys.stderr.write('Boundary slope annealing coefficient: %s\n' % self.boundary_slope_coef.eval(session=self.sess))

                        if self.state_slope_annealing_rate:
                            sys.stderr.write('State slope annealing coefficient: %s\n' % self.state_slope_coef.eval(session=self.sess))

                        pb = tf.contrib.keras.utils.Progbar(n_pb)

                    loss_total = 0.
                    reg_total = 0.
                    if not self.streaming or self.predict_backward:
                        reconstruction_loss_total = 0.
                    if self.streaming and self.predict_forward:
                        prediction_loss_total = 0.
                    correspondence_loss_total = [0.] * len(self.correspondence_loss_scale)
                    if self.lm_loss_scale:
                        encoder_lm_loss_total = [0.] * self.layers_encoder
                    if self.speaker_adversarial_gradient_scale:
                        encoder_speaker_adversarial_loss_total = [0.] * self.layers_encoder
                    if self.passthru_adversarial_gradient_scale and self.n_passthru_neurons:
                        encoder_passthru_adversarial_loss_total = [0.] * self.layers_encoder

                    # Collect correspondence targets if necessary
                    if self.static_correspondence_targets() and (segment_embeddings is None or segment_spans is None):
                        segment_embeddings, segment_spans = self.collect_correspondence_targets(data=train_data)

                    data_feed_train = train_data.get_data_feed(
                        self.train_data_name,
                        minibatch_size=minibatch_size,
                        randomize=True,
                        n_samples=self.n_samples
                    )
                    i_pb_base = 0

                    for i, batch in enumerate(data_feed_train):
                        if self.streaming:
                            X_batch = batch['X']
                            X_batch
                            X_mask_batch = batch['X_mask']
                            y_bwd_batch = batch['y_bwd']
                            y_bwd_mask_batch = batch['y_bwd_mask']
                            y_fwd_batch = batch['y_fwd']
                            y_fwd_mask_batch = batch['y_fwd_mask']
                            speaker_batch = batch['speaker']
                            fixed_boundaries_batch = batch['fixed_boundaries']
                            if self.oracle_boundaries:
                                oracle_boundaries_batch = batch[self.oracle_boundaries + '_boundaries']

                            if self.min_len:
                                minibatch_num = self.global_batch_step.eval(self.sess)
                                n_steps = X_batch.shape[1]
                                start_ix = max(0, n_steps - (self.min_len + int(math.floor(minibatch_num / self.curriculum_steps))))

                                X_batch = X_batch[:,start_ix:]
                                X_mask_batch = X_mask_batch[:,start_ix:]
                                if self.oracle_boundaries:
                                    oracle_boundaries_batch = oracle_boundaries_batch[:,start_ix:]
                                if fixed_boundaries_batch is not None:
                                    fixed_boundaries_batch = fixed_boundaries_batch[:,start_ix:]

                            i_pb = i - i_pb_base
                        else:
                            X_batch = batch['X']
                            X_mask_batch = batch['X_mask']
                            y_bwd_batch = batch['y']
                            y_bwd_mask_batch = batch['y_mask']
                            speaker_batch = batch['speaker']
                            fixed_boundaries_batch = batch['fixed_boundaries']
                            if self.oracle_boundaries:
                                oracle_boundaries_batch = batch[self.oracle_boundaries + '_boundaries']
                            i_pb = i

                        fd_minibatch = {
                            self.X: X_batch,
                            self.X_mask: X_mask_batch,
                            self.speaker: speaker_batch
                        }

                        if not self.streaming or self.predict_backward:
                            fd_minibatch[self.y_bwd] = y_bwd_batch
                            fd_minibatch[self.y_bwd_mask] = y_bwd_mask_batch
                        if self.streaming and self.predict_forward:
                            fd_minibatch[self.y_fwd] = y_fwd_batch
                            fd_minibatch[self.y_fwd_mask] = y_fwd_mask_batch

                        # Feed correspondence targets if necessary
                        if self.static_correspondence_targets():
                            for l in range(len(segment_embeddings)):
                                fd_minibatch[self.correspondence_hidden_state_placeholders[l]] = segment_embeddings[l]
                                fd_minibatch[self.correspondence_feature_placeholders[l]] = segment_spans[l]

                        if self.oracle_boundaries:
                            fd_minibatch[self.oracle_boundaries_placeholder] = oracle_boundaries_batch
                        if fixed_boundaries_batch is not None:
                            fd_minibatch[self.fixed_boundaries_placeholder] = fixed_boundaries_batch

                        info_dict = self.run_train_step(fd_minibatch)
                        loss_cur = info_dict['loss']
                        reg_cur = info_dict['regularizer_loss']
                        if not self.streaming or self.predict_backward:
                            reconstruction_loss_cur = info_dict['reconstruction_loss']
                        if self.streaming and self.predict_forward:
                            prediction_loss_cur = info_dict['prediction_loss']
                        if self.use_correspondence_loss:
                            correspondence_loss_cur = [info_dict['correspondence_loss_l%d' % l] if 'correspondence_loss_l%d' % l in info_dict else 0. for l in range(self.layers_encoder - 1)]
                        if self.lm_loss_scale:
                            encoder_lm_loss_cur = [info_dict['encoder_lm_loss_l%d' % l] for l in range(self.layers_encoder)]
                        if self.speaker_adversarial_gradient_scale:
                            encoder_speaker_adversarial_loss_cur = [info_dict['encoder_speaker_adversarial_loss_l%d' % l] for l in range(self.layers_encoder)]
                        if self.passthru_adversarial_gradient_scale and self.n_passthru_neurons:
                            encoder_passthru_adversarial_loss_cur = [info_dict['encoder_passthru_adversarial_loss_l%d' % l] for l in range(self.layers_encoder)]

                        # Collect correspondence targets if necessary
                        if self.static_correspondence_targets():
                            segment_embeddings, segment_spans = self.collect_correspondence_targets(data=train_data)

                        if self.ema_decay:
                            self.sess.run(self.ema_op)
                        if not np.isfinite(loss_cur):
                            loss_cur = 0
                        loss_total += loss_cur
                        reg_total += reg_cur
                        if not self.streaming or self.predict_backward:
                            reconstruction_loss_total += reconstruction_loss_cur
                        if self.streaming and self.predict_forward:
                            prediction_loss_total += prediction_loss_cur
                        if self.use_correspondence_loss:
                            for l in range(len(correspondence_loss_total)):
                                correspondence_loss_total[l] = correspondence_loss_total[l] + correspondence_loss_cur[l]
                        if self.lm_loss_scale:
                            for l in range(self.layers_encoder):
                                encoder_lm_loss_total[l] = encoder_lm_loss_total[l] + encoder_lm_loss_cur[l]
                        if self.speaker_adversarial_gradient_scale:
                            for l in range(self.layers_encoder):
                                encoder_speaker_adversarial_loss_total[l] = encoder_speaker_adversarial_loss_total[l] + encoder_speaker_adversarial_loss_cur[l]
                        if self.passthru_adversarial_gradient_scale and self.n_passthru_neurons:
                            for l in range(self.layers_encoder):
                                encoder_passthru_adversarial_loss_total[l] = encoder_passthru_adversarial_loss_total[l] + encoder_passthru_adversarial_loss_cur[l]

                        if verbose:
                            pb.update(i_pb+1, values=[('loss', loss_cur), ('reg', reg_cur)])

                        self.check_numerics()

                        if self.streaming:
                            evaluate = (self.eval_freq > 0) \
                                       and ((i+1) % self.eval_freq == 0) \
                                       and (self.step.eval(session=self.sess) > self.n_pretrain_steps)
                            save = evaluate or ((self.save_freq > 0) and ((i+1) % self.save_freq == 0))
                            log = (self.log_freq > 0) and ((i+1) % self.log_freq == 0)
                            n_plot_cur = n_plot if log else None
                            verbose_cur = verbose and evaluate

                            self.run_checkpoint(
                                val_data,
                                save=save,
                                log=log,
                                evaluate=evaluate,
                                n_plot=n_plot_cur,
                                loss=loss_total / (i_pb + 1),
                                reg_loss=reg_total / (i_pb + 1),
                                reconstruction_loss=reconstruction_loss_total / (i_pb + 1) if (not self.streaming or self.predict_backward) else None,
                                prediction_loss=prediction_loss_total / (i_pb + 1) if (self.streaming and self.predict_forward) else None,
                                correspondence_loss=[x / (i_pb + 1) for x in correspondence_loss_total] if self.use_correspondence_loss else None,
                                encoder_lm_losses=[x / (i_pb + 1) for x in encoder_lm_loss_total] if self.lm_loss_scale else None,
                                encoder_speaker_adversarial_losses=[x / (i_pb + 1) for x in encoder_speaker_adversarial_loss_total] if self.speaker_adversarial_gradient_scale else None,
                                encoder_passthru_adversarial_losses=[x / (i_pb + 1) for x in encoder_passthru_adversarial_loss_total] if self.passthru_adversarial_gradient_scale else None,
                                ix2label=ix2label,
                                check_numerics=False,
                                verbose=verbose_cur
                            )

                            self.set_update_mode(verbose=False)

                            if self.streaming and (save or evaluate or log):
                                if verbose:
                                    if save:
                                        next_save = self.save_freq
                                    elif self.save_freq:
                                        next_save = self.save_freq - ((i + 1) % self.save_freq)
                                    else:
                                        next_save = np.inf
                                        
                                    if log:
                                        next_log = self.log_freq
                                    elif self.log_freq:
                                        next_log = self.log_freq - ((i + 1) % self.log_freq)
                                    else:
                                        next_log = np.inf

                                    if evaluate:
                                        next_evaluate = self.eval_freq
                                    elif self.eval_freq:
                                        next_evaluate = self.eval_freq - ((i + 1) % self.eval_freq)
                                    else:
                                        next_evaluate = np.inf
                                        
                                        
                                    n_pb = min(
                                        n_minibatch - (i+1),
                                        next_save,
                                        next_log,
                                        next_evaluate
                                    )

                                    if n_pb > 1:
                                        sys.stderr.write('Running minibatches %d-%d (out of %d)...\n' % (i + 2, i + 1 + n_pb, n_minibatch))
                                    else:
                                        sys.stderr.write('Running minibatch %d (out of %d)...\n' % (i + 1 + n_pb, n_minibatch))

                                    stderr('Update mode: %s\n' % self.update_mode)

                                    if self.optim_name is not None and self.lr_decay_family is not None:
                                        sys.stderr.write('Learning rate: %s\n' % self.lr.eval(session=self.sess))

                                    if self.curriculum_type:
                                        if self.curriculum_type.lower() == 'hard':
                                            sys.stderr.write('Curriculum window length: %s\n' % self.curriculum_t.eval(session=self.sess))
                                        if self.curriculum_type.lower() == 'exp':
                                            sys.stderr.write('Curriculum decay rate: %s\n' % (1. / self.curriculum_t.eval(session=self.sess)))
                                        else:
                                            sys.stderr.write('Curriculum window soft bound location: %s\n' % self.curriculum_t.eval(session=self.sess))

                                    if self.boundary_slope_annealing_rate:
                                        sys.stderr.write('Boundary slope annealing coefficient: %s\n' % self.boundary_slope_coef.eval(session=self.sess))

                                    if self.state_slope_annealing_rate:
                                        sys.stderr.write('State slope annealing coefficient: %s\n' % self.state_slope_coef.eval(session=self.sess))

                                    if self.n_pretrain_steps and (self.step.eval(session=self.sess) <= self.n_pretrain_steps):
                                        sys.stderr.write('Pretraining decoder...\n')

                                    pb = tf.contrib.keras.utils.Progbar(n_pb)

                                loss_total = 0.
                                reg_total = 0.
                                if not self.streaming or self.predict_backward:
                                    reconstruction_loss_total = 0.
                                if self.streaming and self.predict_forward:
                                    prediction_loss_total = 0.
                                if self.use_correspondence_loss:
                                    correspondence_loss_total = [0.] * len(self.correspondence_loss_scale)
                                if self.lm_loss_scale:
                                    encoder_lm_loss_total = [0.] * self.layers_encoder
                                if self.speaker_adversarial_gradient_scale:
                                    encoder_speaker_adversarial_loss_total = [0.] * self.layers_encoder
                                if self.passthru_adversarial_gradient_scale and self.n_passthru_neurons:
                                    encoder_passthru_adversarial_loss_total = [0.] * self.layers_encoder
                                i_pb_base = i+1

                    loss_total /= n_pb
                    reg_total /= n_pb
                    if not self.streaming or self.predict_backward:
                        reconstruction_loss_total /= n_pb
                    if self.streaming and self.predict_forward:
                        prediction_loss_total /= n_pb
                    for l in range(len(correspondence_loss_total)):
                        correspondence_loss_total[l] = correspondence_loss_total[l] / n_pb
                    if self.lm_loss_scale:
                        for l in range(self.layers_encoder):
                            encoder_lm_loss_total[l] = encoder_lm_loss_total[l] / n_pb
                    if self.speaker_adversarial_gradient_scale:
                        for l in range(self.layers_encoder):
                            encoder_speaker_adversarial_loss_total[l] = encoder_speaker_adversarial_loss_total[l] / n_pb
                    if self.passthru_adversarial_gradient_scale and self.n_passthru_neurons:
                        for l in range(self.layers_encoder):
                            encoder_passthru_adversarial_loss_total[l] = encoder_passthru_adversarial_loss_total[l] / n_pb

                    self.sess.run(self.incr_global_step)

                    if self.streaming:
                        save = True
                        evaluate = True
                        n_plot_cur = n_plot
                        log = True

                        if self.eval_freq:
                            eval_freq = self.eval_freq
                        else:
                            eval_freq = np.inf
                        if self.save_freq:
                            save_freq = self.save_freq
                        else:
                            save_freq = np.inf
                        if self.log_freq:
                            log_freq = self.log_freq
                        else:
                            log_freq = np.inf

                        n_pb = min(n_minibatch, eval_freq, save_freq, log_freq)

                    else:
                        step = self.step.eval(session=self.sess)
                        save = self.save_freq > 0 and step % self.save_freq == 0
                        evaluate = self.eval_freq and step % self.eval_freq == 0
                        log = self.log_freq and step % self.log_freq == 0
                        n_plot_cur = n_plot if save else None

                    self.run_checkpoint(
                        val_data,
                        save=save,
                        evaluate=evaluate,
                        log=log,
                        loss=loss_total,
                        reconstruction_loss=reconstruction_loss_total if (not self.streaming or self.predict_backward) else None,
                        prediction_loss=prediction_loss_total if (self.streaming and self.predict_forward) else None,
                        correspondence_loss=correspondence_loss_total,
                        encoder_lm_losses=encoder_lm_loss_total if self.lm_loss_scale else None,
                        encoder_speaker_adversarial_losses=encoder_speaker_adversarial_loss_total if self.speaker_adversarial_gradient_scale else None,
                        encoder_passthru_adversarial_losses=encoder_passthru_adversarial_loss_total if self.passthru_adversarial_gradient_scale else None,
                        reg_loss=reg_total,
                        ix2label=ix2label,
                        n_plot=n_plot_cur,
                        verbose=verbose
                    )

                    if verbose:
                        t1_iter = time.time()
                        time_str = pretty_print_seconds(t1_iter - t0_iter)
                        sys.stderr.write('Iteration time: %s\n' % time_str)

    def plot_utterances(
            self,
            data,
            n_plot=10,
            ix2label=None,
            training=False,
            plot_attn=True,
            plot_positional_encodings=False,
            segtype=None,
            invert_spectrograms=True,
            verbose=True
    ):
        seg = 'hmlstm' in self.encoder_type.lower()
        if segtype is None:
            segtype = self.segtype

        data_feed = data.get_data_feed(self.plot_data_name, minibatch_size=n_plot, randomize=True)
        batch = next(data_feed)

        if self.streaming:
            X_plot = batch['X']
            X_mask_plot = batch['X_mask']
            fixed_boundaries_plot = batch['fixed_boundaries']
            if self.oracle_boundaries:
                oracle_boundaries_plot = batch[self.oracle_boundaries + '_boundaries']
            speaker_plot = batch['speaker']
            if self.predict_backward:
                targs_bwd = batch['y_bwd']
                y_bwd_mask_plot = batch['y_bwd_mask']
            else:
                targs_bwd = None
            if self.predict_forward:
                targs_fwd = batch['y_fwd']
                y_fwd_mask_plot = batch['y_fwd_mask']
            else:
                targs_fwd = None

            if self.min_len:
                minibatch_num = self.global_batch_step.eval(self.sess)
                n_steps = X_plot.shape[1]
                start_ix = max(0, n_steps - (self.min_len + int(math.floor(minibatch_num / self.curriculum_steps))))

                X_plot = X_plot[:, start_ix:]
                X_mask_plot = X_mask_plot[:, start_ix:]

                if fixed_boundaries_plot is not None:
                    fixed_boundaries_plot = fixed_boundaries_plot[:, start_ix:]

                if self.oracle_boundaries:
                    oracle_boundaries_plot = oracle_boundaries_plot[:, start_ix:]

        else:
            X_plot = batch['X']
            X_mask_plot = batch['X_mask']
            targs_bwd = batch['y']
            y_bwd_mask_plot = batch['y_mask']
            targs_fwd = None
            speaker_plot = batch['speaker']
            fixed_boundaries_plot = None
            if self.oracle_boundaries:
                oracle_boundaries_plot = batch[self.oracle_boundaries + '_boundaries']
            ix = batch['indices']

        with self.sess.as_default():
            with self.sess.graph.as_default():
                self.set_predict_mode(True)

                if verbose:
                    sys.stderr.write('Plotting...\n')

                if self.task.lower() == 'classifier' and ix2label is not None:
                    labels = data.labels(one_hot=False, segment_type=segtype)
                    labels_string = np.vectorize(lambda x: ix2label[x])(labels.astype('int'))
                    titles = labels_string[ix]
                else:
                    titles = [None] * n_plot

                to_run = []
                to_run_names = []

                if not self.streaming or self.predict_backward:
                    to_run.append(self.reconstructions)
                    to_run_names.append('preds_bwd')
                    if plot_attn:
                        for l, attn in enumerate(self.lm_plot_attn_bwd):
                            if attn is not None:
                                to_run.append(attn)
                                to_run_names.append('attn_bwd_l%d' % l)
                        for l, attn in enumerate(self.lm_plot_attn_keys_bwd):
                            if attn is not None:
                                to_run.append(attn)
                                to_run_names.append('attn_keys_bwd_l%d' % l)
                    if plot_positional_encodings and self.pe_bwd is not None:
                        to_run.append(self.pe_bwd)
                        to_run_names.append('pe_bwd')
                if self.streaming and self.predict_forward:
                    to_run.append(self.extrapolations)
                    to_run_names.append('preds_fwd')
                    if plot_attn:
                        for l, attn in enumerate(self.lm_plot_attn_fwd):
                            if attn is not None:
                                to_run.append(attn)
                                to_run_names.append('attn_fwd_l%d' % l)
                        for l, attn in enumerate(self.lm_plot_attn_keys_fwd):
                            if attn is not None:
                                to_run.append(attn)
                                to_run_names.append('attn_keys_fwd_l%d' % l)
                    if plot_positional_encodings and self.pe_fwd is not None:
                        to_run.append(self.pe_fwd)
                        to_run_names.append('pe_fwd')
                if self.streaming and not self.predict_backward and not self.predict_forward and self.lm_loss_scale:
                    if self.lm_order_bwd:
                        to_run.append(self.lm_plot_preds_bwd)
                        to_run_names.append('preds_bwd')
                        to_run.append(self.lm_plot_targs_bwd)
                        to_run_names.append('targs_bwd')
                        targs_bwd = None
                        if plot_attn:
                            for l, attn in enumerate(self.lm_plot_attn_bwd):
                                if attn is not None:
                                    to_run.append(attn)
                                    to_run_names.append('attn_bwd_l%d' % l)
                            for l, attn in enumerate(self.lm_plot_attn_keys_bwd):
                                if attn is not None:
                                    to_run.append(attn)
                                    to_run_names.append('attn_keys_bwd_l%d' % l)
                        if plot_positional_encodings and self.lm_pe_bwd is not None:
                            to_run.append(self.lm_pe_bwd)
                            to_run_names.append('pe_bwd')
                    if self.lm_order_fwd:
                        to_run.append(self.lm_plot_preds_fwd)
                        to_run_names.append('preds_fwd')
                        to_run.append(self.lm_plot_targs_fwd)
                        to_run_names.append('targs_fwd')
                        if plot_attn:
                            for l, attn in enumerate(self.lm_plot_attn_fwd):
                                if attn is not None:
                                    to_run.append(attn)
                                    to_run_names.append('attn_fwd_l%d' % l)
                            for l, attn in enumerate(self.lm_plot_attn_keys_fwd):
                                if attn is not None:
                                    to_run.append(attn)
                                    to_run_names.append('attn_keys_fwd_l%d' % l)
                        targs_fwd = None
                        if plot_positional_encodings and self.lm_pe_fwd is not None:
                            to_run.append(self.lm_pe_fwd)
                            to_run_names.append('pe_fwd')

                if seg:
                    if self.boundary_prob_smoothing:
                        to_run += [self.segmentation_probs, self.segmentation_probs_smoothed, self.segmentations, self.encoder_features]
                        to_run_names += ['segmentation_probs', 'segmentation_probs_smoothed', 'segmentations', 'encoder_hidden_states']
                    else:
                        to_run += [self.segmentation_probs, self.segmentations, self.encoder_features]
                        to_run_names += ['segmentation_probs', 'segmentations', 'encoder_hidden_states']

                if self.pad_seqs:
                    fd_minibatch = {
                        self.X: X_plot,
                        self.X_mask: X_mask_plot,
                        self.training: False,
                        self.speaker: speaker_plot
                    }

                    if fixed_boundaries_plot is not None:
                        fd_minibatch[self.fixed_boundaries_placeholder] = fixed_boundaries_plot

                    if self.oracle_boundaries:
                        fd_minibatch[self.oracle_boundaries_placeholder] = oracle_boundaries_plot

                    if self.streaming:
                        fd_minibatch[self.fixed_boundaries_placeholder] = fixed_boundaries_plot
                        if self.predict_backward:
                            fd_minibatch[self.y_bwd] = targs_bwd
                            fd_minibatch[self.y_bwd_mask] = y_bwd_mask_plot
                        if self.predict_forward:
                            fd_minibatch[self.y_fwd] = targs_fwd
                            fd_minibatch[self.y_fwd_mask] = y_fwd_mask_plot

                    else:
                        fd_minibatch[self.y_bwd] = targs_bwd
                        fd_minibatch[self.y_bwd_mask] = y_bwd_mask_plot

                    out_list = self.sess.run(
                        to_run,
                        feed_dict=fd_minibatch
                    )
                else:
                    raise ValueError('``pad_seqs = False`` is broken. Do not use.')

                out = {}
                for i, x in enumerate(out_list):
                    out[to_run_names[i]] = x

                self.set_predict_mode(False)

                if 'preds_bwd' in out:
                    preds_bwd = out['preds_bwd']
                else:
                    preds_bwd = None
                if 'targs_bwd' in out:
                    targs_bwd = out['targs_bwd']
                else:
                    targs_bwd = None
                if 'pe_bwd' in out:
                    pe_bwd = out['pe_bwd']
                else:
                    pe_bwd = None

                if 'preds_fwd' in out:
                    preds_fwd = out['preds_fwd']
                else:
                    preds_fwd = None
                if 'targs_fwd' in out:
                    targs_fwd = out['targs_fwd']
                else:
                    targs_fwd = None
                if 'pe_fwd' in out:
                    pe_fwd = out['pe_fwd']
                else:
                    pe_fwd = None

                if seg:
                    if self.pad_seqs:
                        segmentation_probs = np.stack(out['segmentation_probs'], axis=2)
                        segmentations = np.stack(out['segmentations'], axis=2)
                        states = out['encoder_hidden_states']
                        if self.boundary_prob_smoothing:
                            segmentation_probs_smoothed = np.stack(out['segmentation_probs_smoothed'], axis=2)
                    else:
                        segmentation_probs = []
                        segmentation_probs_smoothed = []
                        segmentations = []
                        for s in out['segmentation_probs']:
                            segmentation_probs.append(np.stack(s, axis=1))
                        for s in out['segmentations']:
                            segmentations.append(np.stack(s, axis=1))
                        states = out['encoder_hidden_states']
                        if self.boundary_prob_smoothing:
                            for s in out['segmentation_probs_smoothed']:
                                segmentation_probs_smoothed.append(np.stack(s, axis=1))
                else:
                    segmentation_probs = None
                    segmentation_probs_smoothed = None
                    segmentations = None
                    states = None

                if self.data_type.lower() == 'acoustic':
                    sr = data.sr
                    hop_length = float(sr) / 1000 * data.offset
                else:
                    sr = 1.
                    hop_length = 1.

                # if self.data_normalization:
                #     shift = batch['shift']
                #     scale = batch['scale']
                #
                #     if shift.shape[1] > 1 and self.streaming and self.min_len:
                #         shift = shift[:, start_ix:]
                #         scale = scale[:, start_ix:]
                #
                #     X_plot = X_plot * scale + shift
                #     if self.residual_targets:
                #         X_plot = X_plot[:, 1:] - X_plot[:, :-1]
                #
                #     if not self.predict_deltas:
                #         shift = shift[..., :self.n_coef]
                #         scale = scale[..., :self.n_coef]
                #     if targs_bwd is not None:
                #         targs_bwd = targs_bwd * scale + shift
                #         if self.residual_targets:
                #             targs_bwd = targs_bwd[:, 1:] - targs_bwd[:, :-1]
                #     if preds_bwd is not None:
                #         preds_bwd = preds_bwd * scale + shift
                #         if self.residual_targets:
                #             preds_bwd = preds_bwd[:, 1:] - preds_bwd[:, :-1]
                #     if targs_fwd is not None:
                #         targs_fwd = targs_fwd * scale + shift
                #         if self.residual_targets:
                #             targs_fwd = targs_fwd[:, 1:] - targs_fwd[:, :-1]
                #     if preds_fwd is not None:
                #         preds_fwd = preds_fwd * scale + shift
                #         if self.residual_targets:
                #             preds_fwd = preds_fwd[:, 1:] - preds_fwd[:, :-1]

                targs = None
                preds = None
                pe = None

                if targs_bwd is not None:
                    if not isinstance(targs_bwd, list):
                        targs_bwd = [targs_bwd]
                    targs_bwd = {'L%d (Backward)' % (l + 1): y for l, y in enumerate(targs_bwd)}
                    if targs is None:
                        targs = targs_bwd
                    else:
                        targs.update(targs_bwd)

                if targs_fwd is not None:
                    if not isinstance(targs_fwd, list):
                        targs_fwd = [targs_fwd]
                    targs_fwd = {'L%d (Forward)' % (l + 1): y for l, y in enumerate(targs_fwd)}
                    if targs is None:
                        targs = targs_fwd
                    else:
                        targs.update(targs_fwd)

                if preds_bwd is not None:
                    if not isinstance(preds_bwd, list):
                        preds_bwd = [preds_bwd]
                    preds_bwd = {'L%d (Backward)' % (l + 1): y for l, y in enumerate(preds_bwd)}
                    if preds is None:
                        preds = preds_bwd
                    else:
                        preds.update(preds_bwd)

                if preds_fwd is not None:
                    if not isinstance(preds_fwd, list):
                        preds_fwd = [preds_fwd]
                    preds_fwd = {'L%d (Forward)' % (l + 1): y for l, y in enumerate(preds_fwd)}
                    if preds is None:
                        preds = preds_fwd
                    else:
                        preds.update(preds_fwd)

                if plot_attn:
                    attn = {}
                    attn_keys = {}
                    for l in range(self.layers_encoder - 1):
                        key_bwd = 'attn_bwd_l%d' % l
                        key_fwd = 'attn_fwd_l%d' % l
                        if key_bwd in out:
                            attn['L%d (Backward)' % (l + 1)] = out[key_bwd]
                        if key_fwd in out:
                            attn['L%d (Forward)' % (l + 1)] = out[key_fwd]

                        key_bwd = 'attn_keys_bwd_l%d' % l
                        key_fwd = 'attn_keys_fwd_l%d' % l
                        if key_bwd in out:
                            attn_keys['L%d (Backward)' % (l + 1)] = out[key_bwd]
                        if key_fwd in out:
                            attn_keys['L%d (Forward)' % (l + 1)] = out[key_fwd]
                else:
                    attn = None
                    attn_keys = None

                if pe_bwd is not None:
                    if not isinstance(pe_bwd, list):
                        pe_bwd = [pe_bwd]
                    pe_bwd = {'L%d (Backward)' % (l + 1): y for l, y in enumerate(pe_bwd)}
                    if pe is None:
                        pe = pe_bwd
                    else:
                        pe.update(pe_bwd)

                if pe_fwd is not None:
                    if not isinstance(pe_fwd, list):
                        pe_fwd = [pe_fwd]
                    pe_fwd = {'L%d (Forward)' % (l + 1): y for l, y in enumerate(pe_fwd)}
                    if pe is None:
                        pe = pe_fwd
                    else:
                        pe.update(pe_fwd)

                plot_keys = ['L%d (Backward)' % (l + 1) for l in range(self.layers_encoder)]
                plot_keys += ['L%d (Forward)' % (l + 1) for l in range(self.layers_encoder)]

                plot_acoustic_features(
                    X_plot if self.data_type.lower() == 'text' else X_plot[..., :data.n_coef],
                    targs=targs,
                    preds=preds,
                    attn=attn,
                    attn_keys=attn_keys,
                    positional_encodings=pe,
                    plot_keys=plot_keys,
                    titles=titles,
                    segmentation_probs=segmentation_probs,
                    segmentation_probs_smoothed=segmentation_probs_smoothed if self.boundary_prob_smoothing else None,
                    segmentations=segmentations,
                    states=states,
                    sr=sr,
                    hop_length=hop_length,
                    label_map=self.label_map,
                    directory=self.outdir
                )

                # if invert_spectrograms and self.spectrogram_inverter is not None and not self.residual_targets:
                #     fps = [1000 / data.offset] * 5
                #     if self.resample_inputs is not None and self.max_len is not None:
                #         fps[0] = fps[0] * self.resample_inputs / self.max_len
                #     if self.resample_targets_bwd:
                #         fps[1] = fps[1] * self.resample_targets_bwd / self.window_len_bwd
                #         fps[2] = fps[2] * self.resample_targets_bwd / self.window_len_bwd
                #     if self.resample_targets_fwd:
                #         fps[3] = fps[3] * self.resample_targets_fwd / self.window_len_fwd
                #         fps[4] = fps[4] * self.resample_targets_fwd / self.window_len_fwd
                #
                #     self.spectrogram_inverter(
                #         input=X_plot[..., :data.n_coef],
                #         targets_bwd=targs_bwd,
                #         preds_bwd=preds_bwd,
                #         targets_fwd=targs_fwd,
                #         preds_fwd=preds_fwd,
                #         fps=fps,
                #         sr=sr,
                #         offset=data.offset,
                #         reverse_reconstructions=self.streaming or self.reverse_targets,
                #         dir=self.outdir,
                #     )


    def plot_label_histogram(self, labels_pred, dir=None):
        if dir is None:
            dir = self.outdir

        bins = self.units_encoder[-1]

        if bins < 1000:
            plot_label_histogram(labels_pred, dir=dir, bins=bins)

    def plot_label_heatmap(self, labels, preds, dir=None):
        if dir is None:
            dir = self.outdir

        plot_label_heatmap(
            labels,
            preds,
            dir=dir
        )

    def initialized(self):
        """
        Check whether model has been initialized.

        :return: ``bool``; whether the model has been initialized.
        """
        with self.sess.as_default():
            with self.sess.graph.as_default():
                uninitialized = self.sess.run(self.report_uninitialized)
                if len(uninitialized) == 0:
                    return True
                else:
                    return False

    def save(self, dir=None):

        assert not self.predict_mode, 'Cannot save while in predict mode, since this would overwrite the parameters with their moving averages.'

        if dir is None:
            dir = self.outdir
        with self.sess.as_default():
            with self.sess.graph.as_default():
                failed = True
                i = 0

                # Try/except to handle race conditions in Windows
                while failed and i < 10:
                    try:
                        self.saver.save(self.sess, dir + '/model.ckpt')
                        with open(dir + '/m.obj', 'wb') as f:
                            pickle.dump(self, f)
                        failed = False
                    except Exception:
                        sys.stderr.write('Write failure during save. Retrying...\n')
                        time.sleep(1)
                        i += 1
                if i >= 10:
                    sys.stderr.write('Could not save model to checkpoint file. Saving to backup...\n')
                    self.saver.save(self.sess, dir + '/model_backup.ckpt')
                    with open(dir + '/m.obj', 'wb') as f:
                        pickle.dump(self, f)

    def load(self, outdir=None, predict=False, restore=True, allow_missing=True):
        """
        Load weights from a DNN-Seg checkpoint and/or initialize the DNN-Seg model.
        Missing weights in the checkpoint will be kept at their initializations, and unneeded weights in the checkpoint will be ignored.

        :param outdir: ``str``; directory in which to search for weights. If ``None``, use model defaults.
        :param predict: ``bool``; load EMA weights because the model is being used for prediction. If ``False`` load training weights.
        :param restore: ``bool``; restore weights from a checkpoint file if available, otherwise initialize the model. If ``False``, no weights will be loaded even if a checkpoint is found.
        :param allow_missing: ``bool``; load all weights found in the checkpoint file, allowing those that are missing to remain at their initializations. If ``False``, weights in checkpoint must exactly match those in the model graph, or else an error will be raised. Leaving set to ``True`` is helpful for backward compatibility, setting to ``False`` can be helpful for debugging.
        :return:
        """
        if outdir is None:
            outdir = self.outdir
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if not self.initialized():
                    self.sess.run(tf.global_variables_initializer())
                    tf.tables_initializer().run()
                if restore and os.path.exists(outdir + '/checkpoint'):
                    self._restore_inner(outdir + '/model.ckpt', predict=predict, allow_missing=allow_missing)
                else:
                    if predict:
                        sys.stderr.write('No EMA checkpoint available. Leaving internal variables unchanged.\n')

    def set_update_mode(self, verbose=False):
        if isinstance(self.weight_update_mode, str):
            if self.weight_update_mode in ['all', 'state', 'boundary']:
                update_mode = self.weight_update_mode
            elif self.weight_update_mode.lower().startswith('alternating'):
                freq = int(self.weight_update_mode[11:])
                if (self.global_batch_step.eval(self.sess) // freq) % 2 == 0:
                    update_mode = 'state'
                else:
                    update_mode = 'boundary'
            elif self.weight_update_mode.lower().startswith('layerwise'):
                def mod_map(l):
                    if l % 2 == 0:
                        key = 'state_'
                    else:
                        key = 'boundary_'
                    key +=  str(l // 2)

                    return key

                freq = int(self.weight_update_mode[9:])
                mod = self.layers_encoder * 2 - 1

                update_mode = mod_map(
                    (self.global_batch_step.eval(self.sess) // freq) % mod
                )
            else:
                raise ValueError('Unrecognized update mode %s' % self.weight_update_mode)

        else:
            prob = float(self.weight_update_mode)
            update_state = random.random() < prob
            if update_state:
                update_mode = 'state'
            else:
                update_mode = 'boundary'

        if verbose:
            sys.stderr.write('Update mode: %s\n' % update_mode)
            sys.stderr.flush()

        self.update_mode = update_mode

    def set_predict_mode(self, mode):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if self.ema_decay:
                    reload = mode != self.predict_mode
                    if reload:
                        self.load(predict=mode)

                self.predict_mode = mode

    def report_settings(self, indent=0):
        out = ' ' * indent + 'MODEL SETTINGS:\n'
        for kwarg in UNSUPERVISED_WORD_CLASSIFIER_INITIALIZATION_KWARGS:
            val = getattr(self, kwarg.key)
            out += ' ' * (indent + 2) + '%s: %s\n' %(kwarg.key, "\"%s\"" %val if isinstance(val, str) else val)

        return out

    def report_n_params(self, indent=0):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                n_params = 0
                var_names = [v.name for v in tf.trainable_variables()]
                var_vals = self.sess.run(tf.trainable_variables())
                out = ' ' * indent + 'TRAINABLE PARAMETERS:\n'
                for i in range(len(var_names)):
                    v_name = var_names[i]
                    v_val = var_vals[i]
                    cur_params = np.prod(np.array(v_val).shape)
                    n_params += cur_params
                    out += ' ' * indent + '  ' + v_name.split(':')[0] + ': %s\n' % str(cur_params)
                out += ' ' * indent + '  TOTAL: %d\n\n' % n_params

                return out

    # TODO: Complete this method
    def summary(self):
        out = ''

        return out


class DNNSegMLE(DNNSeg):
    _INITIALIZATION_KWARGS = UNSUPERVISED_WORD_CLASSIFIER_MLE_INITIALIZATION_KWARGS

    _doc_header = """
        MLE implementation of unsupervised word classifier.

    """
    _doc_args = DNNSeg._doc_args
    _doc_kwargs = DNNSeg._doc_kwargs
    _doc_kwargs += '\n' + '\n'.join([' ' * 8 + ':param %s' % x.key + ': ' + '; '.join(
        [x.dtypes_str(), x.descr]) + ' **Default**: ``%s``.' % (x.default_value if not isinstance(x.default_value,
                                                                                                  str) else "'%s'" % x.default_value)
                                     for x in _INITIALIZATION_KWARGS])
    __doc__ = _doc_header + _doc_args + _doc_kwargs

    def __init__(self, train_data, **kwargs):
        super(DNNSegMLE, self).__init__(
            train_data,
            **kwargs
        )

        for kwarg in DNNSegMLE._INITIALIZATION_KWARGS:
            setattr(self, kwarg.key, kwargs.pop(kwarg.key, kwarg.default_value))

        kwarg_keys = [x.key for x in DNNSeg._INITIALIZATION_KWARGS]
        for kwarg_key in kwargs:
            if kwarg_key not in kwarg_keys:
                raise TypeError('__init__() got an unexpected keyword argument %s' %kwarg_key)

        self._initialize_metadata()

    def _initialize_metadata(self):
        super(DNNSegMLE, self)._initialize_metadata()

    def _pack_metadata(self):
        md = super(DNNSegMLE, self)._pack_metadata()

        for kwarg in DNNSegMLE._INITIALIZATION_KWARGS:
            md[kwarg.key] = getattr(self, kwarg.key)

        return md

    def _unpack_metadata(self, md):
        super(DNNSegMLE, self)._unpack_metadata(md)

        for kwarg in DNNSegMLE._INITIALIZATION_KWARGS:
            setattr(self, kwarg.key, md.pop(kwarg.key, kwarg.default_value))

        if len(md) > 0:
            sys.stderr.write('Saved model contained unrecognized attributes %s which are being ignored\n' %sorted(list(md.keys())))

    def _initialize_classifier(self, classifier_in):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                encoding_logits = classifier_in[..., :self.encoding_n_dims]

                if self.task == 'classifier':
                    if self.binary_classifier:
                        if self.state_slope_annealing_rate:
                            rate = self.state_slope_annealing_rate
                            if self.slope_annealing_max is None:
                                slope_coef = 1 + rate * tf.cast(self.step, dtype=tf.float32)
                            else:
                                slope_coef = tf.minimum(self.slope_annealing_max, 1 + rate * tf.cast(self.step, dtype=tf.float32))
                            encoding_logits *= slope_coef
                        encoding_probs = tf.sigmoid(encoding_logits)
                        encoding = encoding_probs
                        encoding = get_activation(
                            self.encoder_state_discretizer,
                            training=self.training,
                            session=self.sess,
                            from_logits=False
                        )(encoding)
                        self.encoding_entropy = binary_entropy(encoding_logits, from_logits=True, session=self.sess)
                        self.encoding_entropy_mean = tf.reduce_mean(self.encoding_entropy)
                        self._add_regularization(encoding_logits, self.entropy_regularizer)
                        self._add_regularization(encoding, self.boundary_regularizer)
                    else:
                        encoding = tf.nn.softmax(encoding_logits)
                else:
                    encoding = encoding_logits

                return encoding

    def _initialize_output_model(self):
        if self.data_normalization == 'range' and self.constrain_output:
            distance_func = 'binary_xent'
        elif self.data_type.lower() == 'text':
            distance_func = 'softmax_xent'
        else:
            distance_func = 'l2norm'

        with self.sess.as_default():
            with self.sess.graph.as_default():
                if self.task != 'streaming_autoencoder':
                    if not self.streaming or self.predict_backward:
                        if distance_func == 'binary_xent':
                            self.out_bwd = tf.sigmoid(self.decoder_bwd)
                        elif distance_func == 'softmax_xent':
                            self.out_bwd = tf.nn.softmax(self.decoder_bwd, axis=-1)
                        else:
                            self.out_bwd = self.decoder_bwd

                        self.reconstructions = self.out_bwd
                        if not self.dtw_gamma:
                            self.reconstructions *= self.y_bwd_mask[..., None]

                    if self.streaming and self.predict_forward:
                        if distance_func == 'binary_xent':
                            self.out_fwd = tf.sigmoid(self.decoder_fwd)
                        elif distance_func == 'softmax_xent':
                            self.out_fwd = tf.nn.softmax(self.decoder_fwd, axis=-1)
                        else:
                            self.out_fwd = self.decoder_fwd

                        self.extrapolations = self.out_fwd
                        if not self.dtw_gamma:
                            self.extrapolations *= self.y_fwd_mask[..., None]

                    # if self.n_correspondence:
                    #     self.correspondence_autoencoders = []
                    #     for l in range(self.layers_encoder - 1):
                    #         correspondence_autoencoder = self._initialize_decoder(self.encoder_hidden_states[l], self.resample_correspondence)
                    #         self.correspondence_autoencoders.append(correspondence_autoencoder)

    def _lm_loss_inner(self, l, targets, logits, mask):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                if l > 0 and self.encoder_state_discretizer is not None:
                    binary_state = True
                else:
                    binary_state = False

                if l == 0 and self.data_type.lower() == 'text':
                    loss = tf.nn.softmax_cross_entropy_with_logits_v2(
                        labels=targets,
                        logits=logits
                    )[..., None]
                elif binary_state:
                    loss = tf.nn.sigmoid_cross_entropy_with_logits(
                        labels=targets,
                        logits=logits
                    )
                else:
                    if l == 0 and self.speaker_revnet_n_layers and self.data_type.lower() == 'acoustic':
                        logits = self.speaker_revnet.backward(logits, weights=self.speaker_embeddings)
                    loss = (targets - logits) ** 2

                if loss.shape[-1] > 1:
                    condition = tf.tile(
                        tf.cast(mask, dtype=tf.bool)[..., None],
                        [1] * len(mask.shape) + [logits.shape[-1]]
                    )
                else:
                    condition = tf.cast(mask, dtype=tf.bool)[..., None]

                loss = tf.where(condition, loss, tf.zeros_like(loss))
                loss = tf.reduce_sum(loss) / (tf.reduce_sum(mask) * tf.cast(tf.shape(targets)[-1], dtype=self.FLOAT_TF) + self.epsilon)
                # loss = tf.reduce_sum(loss)

                return loss

    def _lm_distance_func(self, l):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                # if l > 0 and (self.encoder_state_discretizer or self.xent_state_predictions):
                if l > 0 and self.xent_state_predictions:
                    binary_state = True
                else:
                    binary_state = False

                if l == 0 and self.data_type.lower() == 'text':
                    distance_func = 'softmax_xent'
                elif binary_state:
                    distance_func = 'binary_xent'
                elif self.l2_normalize_targets:
                    # distance_func = 'arc'
                    distance_func = 'cosine'
                else:
                    distance_func = 'mse'

                return distance_func

    def _apply_dtw(self, l):
        out = False
        if self.use_dtw and self._lm_distance_func(l) in ['mse', 'cosine']:
            out = True
        return out

    def _initialize_objective(self, n_train):
        AMP = 1

        with self.sess.as_default():
            with self.sess.graph.as_default():
                if self.data_normalization == 'range' and self.constrain_output:
                    distance_func = 'binary_xent'
                elif self.data_type.lower() == 'text':
                    distance_func = 'softmax_xent'
                else:
                    distance_func = 'l2norm'

                loss = 0

                if not self.streaming or self.predict_backward:
                    self.encoding_post = self.encoding
                    if self.task == 'classifier':
                        self.labels_post = self.labels
                        self.label_probs_post = self.label_probs

                    targets = self.y_bwd
                    preds = self.decoder_bwd
                    if self.curriculum_t is not None:
                        if self.curriculum_type.lower() == 'hard':
                            targets = targets[..., :self.curriculum_t, :]
                            preds = preds[..., :self.curriculum_t, :]
                            weights = None
                        elif self.curriculum_type.lower() == 'exp':
                            weights = tf.exp(
                                - (tf.range(self.n_timesteps_output_bwd, dtype=self.FLOAT_TF) / self.curriculum_t)
                            )
                            weights = (weights, weights)
                        else:
                            weights = 1 / (1 + tf.exp(
                                self.curriculum_init *
                                (tf.range(self.n_timesteps_output_bwd, dtype=self.FLOAT_TF) - self.curriculum_t))
                            )
                    else:
                        weights = None

                    self.loss_reconstruction = self._get_loss(
                        targets,
                        preds,
                        use_dtw=self.use_dtw,
                        distance_func=distance_func,
                        weights=weights
                    )

                    loss += self.loss_reconstruction

                if self.streaming and self.predict_forward:
                    targets = self.y_fwd
                    preds = self.decoder_fwd
                    if self.curriculum_t is not None:
                        if self.curriculum_type.lower() == 'hard':
                            targets = targets[..., :self.curriculum_t, :]
                            preds = preds[..., :self.curriculum_t, :]
                            weights = None
                        elif self.curriculum_type.lower() == 'exp':
                            weights = tf.exp(
                                - (tf.range(self.n_timesteps_output_fwd, dtype=self.FLOAT_TF) / self.curriculum_t)
                            )
                            weights = (weights, weights)
                        else:
                            weights = 1 / (1 + tf.exp(
                                self.curriculum_init *
                                (tf.range(self.n_timesteps_output_fwd, dtype=self.FLOAT_TF) - self.curriculum_t))
                            )
                    else:
                        weights = None

                    self.loss_prediction = self._get_loss(
                        targets,
                        preds,
                        use_dtw=self.use_dtw,
                        distance_func=distance_func,
                        weights=weights
                    )

                    loss += self.loss_prediction

                if self.use_correspondence_loss:
                    self.correspondence_losses = []

                    if self.n_correspondence:
                        correspondence_ae_losses = self._compute_correspondence_ae_loss(
                            implementation=self.correspondence_loss_implementation,
                            n_timesteps=self.correspondence_n_timesteps,
                            alpha=self.correspondence_alpha
                        )
                        for cae_loss in correspondence_ae_losses:
                            self.correspondence_losses.append(cae_loss)
                            loss += cae_loss
                    else:
                        for l in range(self.layers_encoder - 1):
                            if self.correspondence_loss_scale[l]:
                                correspondence_inputs = self.encoder_embeddings[l]
                                correspondence_targets = self.averaged_inputs[l]
                                if l == 0:
                                    correspondence_weights = self.X_mask
                                else:
                                    correspondence_weights = self.encoder_segmentations[l-1]

                                if self.lm_masking_mode.lower() in ['drop_masked', 'predict_at_boundaries']:
                                    correspondence_mask = tf.cast(tf.round(correspondence_weights), dtype=tf.bool)
                                    correspondence_inputs = tf.boolean_mask(
                                        correspondence_inputs,
                                        correspondence_mask
                                    )
                                    correspondence_targets = tf.boolean_mask(
                                        correspondence_targets,
                                        correspondence_mask
                                    )
                                    correspondence_weights = tf.boolean_mask(
                                        correspondence_weights,
                                        correspondence_mask
                                    )
                                    if self.round_loss_weights:
                                        if self.backprop_into_loss_weights:
                                            correspondence_weights = round_straight_through(correspondence_weights, session=self.sess)
                                        else:
                                            correspondence_weights = tf.cast(correspondence_mask, dtype=self.FLOAT_TF)
                                    elif not self.backprop_into_loss_weights:
                                        correspondence_weights = tf.stop_gradient(correspondence_weights)

                                if self.correspondence_gradient_scale[l] is not None:
                                    correspondence_inputs = replace_gradient(
                                        tf.identity,
                                        lambda x: x * self.correspondence_gradient_scale[l]
                                    )(correspondence_inputs)
                                    correspondence_targets = replace_gradient(
                                        tf.identity,
                                        lambda x: x * self.correspondence_gradient_scale[l]
                                    )(correspondence_targets)

                                correspondence_logits = correspondence_inputs

                                # if not self.backprop_into_targets:
                                if True:
                                    correspondence_targets = tf.stop_gradient(correspondence_targets)

                                if l == 0:
                                    pred_dim = self.X.shape[-1]
                                elif self.features_encoder[l-1] is None:
                                    pred_dim = self.units_encoder[l-1]
                                else:
                                    pred_dim = self.features_encoder[l-1]

                                for m in range(self.layers_correspondence_decoder):
                                    if m == self.layers_correspondence_decoder - 1:
                                        units_cur = pred_dim
                                        activation = None
                                    else:
                                        units_cur = self.units_correspondence_decoder[m]
                                        activation = 'elu'

                                    correspondence_logits = DenseLayer(
                                        training=self.training,
                                        units=units_cur,
                                        activation=activation,
                                        batch_normalization_decay=self.decoder_batch_normalization_decay,
                                        session=self.sess,
                                        name='correspondence_decoder_l%d_i%d' % (l, m)
                                    )(correspondence_logits)

                                if l == 0 and self.speaker_revnet_n_layers:
                                    correspondence_logits = self.speaker_revnet.backward(correspondence_logits, weights=self.speaker_embeddings)

                                cae_loss = self._get_loss(
                                    correspondence_targets,
                                    correspondence_logits,
                                    use_dtw=False,
                                    distance_func=self._lm_distance_func(l),
                                    weights=correspondence_weights,
                                    reduce=True,
                                    name='cae_loss_L%d' % l
                                ) * self.correspondence_loss_scale[l]

                                self.correspondence_losses.append(cae_loss)

                                loss += cae_loss

                if self.lm_loss_scale:
                    assert not self.residual_targets, 'residual_targets is currently broken. Do not use.'

                    self.lm_losses_bwd = []
                    self.lm_losses_fwd = []
                    self.lm_losses = []

                    lm_losses = []

                    for l in range(self.layers_encoder - 1, -1, -1):
                        loss_scale = self.lm_loss_scale[l]

                        if loss_scale:
                            if self.lm_targets_bwd[l] is not None and self.lm_order_bwd:
                                logits_bwd = self.lm_logits_bwd[l]
                                targets_bwd = self.lm_targets_bwd[l]
                                weights_bwd = self.lm_weights_bwd[l]

                                lm_loss_bwd_cur = self._get_loss(
                                    targets_bwd,
                                    logits_bwd,
                                    use_dtw=self._apply_dtw(l),
                                    distance_func=self._lm_distance_func(l),
                                    weights=weights_bwd,
                                    reduce=True,
                                    name='lm_bwd_loss_L%d' % l
                                ) * loss_scale
                                lm_losses.insert(0, lm_loss_bwd_cur)
                            else:
                                lm_loss_bwd_cur = 0.

                            if self.lm_targets_fwd[l] is not None and self.lm_order_fwd:
                                logits_fwd = self.lm_logits_fwd[l]
                                targets_fwd = self.lm_targets_fwd[l]
                                weights_fwd = self.lm_weights_fwd[l]

                                lm_loss_fwd_cur = self._get_loss(
                                    targets_fwd,
                                    logits_fwd,
                                    use_dtw=self._apply_dtw(l),
                                    distance_func=self._lm_distance_func(l),
                                    weights=weights_fwd,
                                    reduce=True,
                                    name='lm_fwd_loss_L%d' % l
                                ) * loss_scale
                                lm_losses.insert(0, lm_loss_fwd_cur)
                            else:
                                lm_loss_fwd_cur = 0.

                            lm_loss_cur = lm_loss_bwd_cur + lm_loss_fwd_cur
                        else:
                            lm_loss_cur = 0.

                        self.lm_losses.insert(0, lm_loss_cur)
                    #
                    # loss_bwd = tf.stack(self.lm_losses_bwd, axis=-1)
                    # loss_total_bwd = tf.reduce_sum(loss_bwd, axis=-1, keepdims=True)
                    # loss_weights_bwd = loss_bwd / tf.maximum(loss_total_bwd, self.epsilon)
                    # loss_bwd_weighted = tf.reduce_sum(loss_bwd * loss_weights_bwd, axis=-1)
                    # loss_fwd = tf.stack(self.lm_losses_fwd, axis=-1)
                    # loss_total_fwd = tf.reduce_sum(loss_fwd, axis=-1, keepdims=True)
                    # loss_weights_fwd = loss_fwd / tf.maximum(loss_total_fwd, self.epsilon)
                    # loss_fwd_weighted = tf.reduce_sum(loss_fwd * loss_weights_fwd, axis=-1)
                    lm_losses = tf.stack(lm_losses, axis=-1)
                    lm_losses_total = tf.reduce_sum(lm_losses, axis=-1, keepdims=True)
                    if self.normalize_lm_losses:
                        lm_loss_weights = lm_losses / tf.maximum(lm_losses_total, self.epsilon)
                        lm_losses_weighted = tf.reduce_sum(lm_losses * lm_loss_weights, axis=-1)
                    else:
                        lm_losses_weighted = lm_losses_total[0]

                    # lm_losses_weighted = tf.Print(lm_losses_weighted, [lm_losses, lm_losses_total, lm_loss_weights, lm_losses_weighted])

                    loss += lm_losses_weighted

                if self.speaker_adversarial_gradient_scale:
                    speaker_adversarial_loss = 0.
                    self.encoder_speaker_adversarial_losses = []
                    for l in range(self.layers_encoder):

                        L = self.speaker_adversarial_gradient_scale
                        speaker_pred = self.encoder_features[l]
                        speaker_pred = replace_gradient(
                            tf.identity,
                            lambda x: -(x * L)
                        )(speaker_pred)

                        if l < self.layers_encoder - 1:
                            units = self.features_encoder[l]
                        else:
                            units = self.features_encoder[-1]

                        speaker_pred = RNNLayer(
                            units=units,
                            activation=self.encoder_inner_activation,
                            recurrent_activation=self.encoder_recurrent_activation,
                            return_sequences=False,
                            name='speaker_classifier_rnn_l%d' % l
                        )(speaker_pred)

                        speaker_pred = DenseLayer(
                            units=len(self.speaker_list),
                            name='speaker_classifier_final_l%d' % l
                        )(speaker_pred)

                        targets = self.speaker_one_hot[..., :-1]

                        speaker_classifier_loss = tf.losses.softmax_cross_entropy(
                            targets,
                            speaker_pred
                        )

                        self.encoder_speaker_adversarial_losses.append(speaker_classifier_loss)

                        speaker_adversarial_loss += speaker_classifier_loss

                if self.passthru_adversarial_gradient_scale:
                    passthru_adversarial_loss = 0.
                    self.encoder_passthru_adversarial_losses = []

                    passthru_targets = tf.stop_gradient(self.passthru_neurons)

                    for l in range(self.layers_encoder):
                        L = self.passthru_adversarial_gradient_scale
                        passthru_preds = self.encoder_features[l]
                        passthru_preds = replace_gradient(
                            tf.identity,
                            lambda x: -(x * L)
                        )(passthru_preds)

                        if l < self.layers_encoder - 1:
                            units = self.features_encoder[l]
                        else:
                            units = self.features_encoder[-1]

                        passthru_preds = RNNLayer(
                            units=units,
                            activation=self.encoder_inner_activation,
                            recurrent_activation=self.encoder_recurrent_activation,
                            return_sequences=True,
                            name='passthru_regression_rnn_l%d' % l
                        )(passthru_preds)

                        passthru_preds = DenseLayer(
                            units=self.n_passthru_neurons,
                            name='passthru_regression_final_l%d' % l
                        )(passthru_preds)

                        passthru_adversarial_loss_cur = self._get_loss(
                            passthru_targets,
                            passthru_preds,
                            use_dtw=False,
                            distance_func='mse',
                            reduce=True
                        )

                        self.encoder_passthru_adversarial_losses.append(passthru_adversarial_loss_cur)

                        passthru_adversarial_loss += passthru_adversarial_loss_cur

                self.full_loss = loss

                if len(self.regularizer_map) > 0:
                    self.regularizer_loss_total = self._apply_regularization(normalized=True)
                    self.full_loss += self.regularizer_loss_total
                else:
                    self.regularizer_loss_total = tf.constant(0., dtype=self.FLOAT_TF)

                if self.speaker_adversarial_gradient_scale:
                    self.full_loss += speaker_adversarial_loss

                if self.passthru_adversarial_gradient_scale:
                    self.full_loss += passthru_adversarial_loss

                self.loss = loss
                self.optim = self._initialize_optimizer(self.optim_name)

                boundary_var_re = re.compile('boundary')
                state_var_re = re.compile('hmlstm_encoder/(bottomup|recurrent|topdown)')
                
                trainable_variables = tf.trainable_variables()

                varset = {
                    'boundary': [x for x in trainable_variables if not state_var_re.search(x.name)],
                    'state': [x for x in trainable_variables if not boundary_var_re.search(x.name)]
                }

                for l in range(self.layers_encoder):
                    s_key = 'state_%d' % l
                    b_key = 'boundary_%d' % l
                    l_str = '_l%d_' % l
                    
                    varset[s_key] = []
                    varset[b_key] = []

                    for x in trainable_variables:
                        s_match = state_var_re.search(x.name)
                        b_match = boundary_var_re.search(x.name)
                        if s_match:
                            if l_str in x.name:
                                varset[s_key].append(x)
                        elif b_match:
                            if l_str in x.name:
                                varset[b_key].append(x)
                        else:
                            varset[s_key].append(x)
                            varset[b_key].append(x)

                if self.weight_update_mode.lower() == 'all':
                    train_ops = {
                        'all': self.optim.minimize(self.full_loss, global_step=self.global_batch_step)
                    }
                elif self.weight_update_mode.lower() == 'boundary':
                    train_ops = {
                        'boundary': self.optim.minimize(self.full_loss, global_step=self.global_batch_step, var_list=varset['boundary'])
                    }
                elif self.weight_update_mode.lower() == 'state':
                    train_ops = {
                        'state': self.optim.minimize(self.full_loss, global_step=self.global_batch_step, var_list=varset['state'])
                    }
                elif self.weight_update_mode.lower().startswith('alternating'):
                    train_ops = {
                        'boundary': self.optim.minimize(self.full_loss, global_step=self.global_batch_step, var_list=varset['boundary']),
                        'state': self.optim.minimize(self.full_loss, global_step=self.global_batch_step, var_list=varset['state'])
                    }
                elif self.weight_update_mode.lower().startswith('layerwise'):
                    layer_matcher = re.compile('\_l[0-9]+\_')
                    for k in varset:
                        if layer_matcher.search(k):
                            self.train_ops[k] = self.optim.minimize(self.full_loss, global_step=self.global_batch_step, var_list=varset[k])
                else:
                    float(self.weight_update_mode)
                    train_ops = {
                        'boundary': self.optim.minimize(self.full_loss, global_step=self.global_batch_step, var_list=varset['boundary']),
                        'state': self.optim.minimize(self.full_loss, global_step=self.global_batch_step, var_list=varset['state'])
                    }

                self.train_ops = train_ops

    def run_train_step(
            self,
            feed_dict,
            return_loss=True,
            return_regularizer_loss=True,
            return_reconstructions=False,
            return_labels=False,
            return_label_probs=False,
            return_encoding_entropy=False,
            return_segmentation_probs=False
    ):
        with self.sess.as_default():
            with self.sess.graph.as_default():
                out_dict = {}

                if return_loss or return_reconstructions or return_labels or return_label_probs:
                    train_op = self.train_ops[self.update_mode]

                    to_run = [train_op]
                    to_run_names = []
                    if return_loss:
                        to_run.append(self.loss)
                        to_run_names.append('loss')
                        if self.streaming:
                            if self.predict_backward:
                                to_run.append(self.loss_reconstruction)
                                to_run_names.append('reconstruction_loss')
                            if self.predict_forward:
                                to_run.append(self.loss_prediction)
                                to_run_names.append('prediction_loss')
                        else:
                            to_run.append(self.loss_reconstruction)
                            to_run_names.append('reconstruction_loss')
                        if self.use_correspondence_loss:
                            for l in range(self.layers_encoder - 1):
                                if self.correspondence_loss_scale[l]:
                                    to_run.append(self.correspondence_losses[l])
                                    to_run_names.append('correspondence_loss_l%d' % l)
                        if self.lm_loss_scale:
                            for l in range(self.layers_encoder):
                                to_run.append(self.lm_losses[l])
                                to_run_names.append('encoder_lm_loss_l%d' % l)
                        if self.speaker_adversarial_gradient_scale:
                            for l in range(self.layers_encoder):
                                to_run.append(self.encoder_speaker_adversarial_losses[l])
                                to_run_names.append('encoder_speaker_adversarial_loss_l%d' % l)
                        if self.speaker_adversarial_gradient_scale:
                            for l in range(self.layers_encoder):
                                to_run.append(self.encoder_speaker_adversarial_losses[l])
                                to_run_names.append('encoder_speaker_adversarial_loss_l%d' % l)
                        if self.passthru_adversarial_gradient_scale:
                            for l in range(self.layers_encoder):
                                to_run.append(self.encoder_passthru_adversarial_losses[l])
                                to_run_names.append('encoder_passthru_adversarial_loss_l%d' % l)
                    if return_regularizer_loss:
                        to_run.append(self.regularizer_loss_total)
                        to_run_names.append('regularizer_loss')
                    if return_reconstructions:
                        to_run.append(self.reconstructions)
                        to_run_names.append('reconst')
                    if return_labels:
                        to_run.append(self.labels)
                        to_run_names.append('labels')
                    if return_label_probs:
                        to_run.append(self.label_probs)
                        to_run_names.append('label_probs')
                    if return_encoding_entropy:
                        to_run.append(self.encoding_entropy_mean)
                        to_run_names.append('encoding_entropy')
                    if self.encoder_type.lower() in ['cnn_hmlstm', 'hmlstm'] and return_segmentation_probs:
                        to_run.append(self.segmentation_probs)
                        to_run_names.append('segmentation_probs')
                    if self.task == 'streaming_autoencoder':
                        to_run.append(self.encoder_states)
                        to_run.append(self.segmentation_probs)
                        to_run.append(self.out_left)
                        to_run.append(self.out_right)
                        to_run_names.append('encoder_states')
                        to_run_names.append('segmentation_probs')
                        to_run_names.append('out_left')
                        to_run_names.append('out_right')
                    if self.n_correspondence and not self.correspondence_live_targets: # Collect correspondence targets
                        [to_run.append(seg_states) for seg_states in self.correspondence_hidden_states]
                        [to_run.append(seg_feats) for seg_feats in self.correspondence_feats]
                        [to_run.append(seg_feats_mask) for seg_feats_mask in self.correspondence_mask]
                        [to_run_names.append('correspondence_seg_states_l%d' %i) for i in range(len(self.correspondence_hidden_states))]
                        [to_run_names.append('correspondence_seg_feats_l%d' %i) for i in range(len(self.correspondence_feats))]
                        [to_run_names.append('correspondence_seg_feats_mask_l%d' %i) for i in range(len(self.correspondence_mask))]

                    output = self.sess.run(to_run, feed_dict=feed_dict)

                    for i, x in enumerate(output[1:]):
                        out_dict[to_run_names[i]] = x

                return out_dict

    def report_settings(self, indent=0):
        out = super(DNNSegMLE, self).report_settings(indent=indent)
        for kwarg in UNSUPERVISED_WORD_CLASSIFIER_MLE_INITIALIZATION_KWARGS:
            val = getattr(self, kwarg.key)
            out += ' ' * indent + '  %s: %s\n' %(kwarg.key, "\"%s\"" %val if isinstance(val, str) else val)

        out += '\n'

        return out





