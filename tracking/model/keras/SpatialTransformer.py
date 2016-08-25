# -*- coding: utf-8 -*-
"""
Created on Wed Aug 17 21:24:21 2016

@author: MindLAB
"""

from theano import tensor as T
from keras import backend as K
from keras.engine.topology import Layer
from tracking.util.theano.Data import Data


class SpatialTransformer(Layer):
    
    def __init__(self, layers, downsampleFactor=1):
        self.downsampleFactor = downsampleFactor
        super(SpatialTransformer, self).__init__()
        
        if layers:
            node_indices = [0 for _ in range(len(layers))]
            self.add_inbound_node(layers, node_indices, None)
            self.built = True
        else:
            self.built = False

    def call(self, X, mask=None):
        if type(X) is not list or len(X) != 2:
            raise Exception("Transformer must be called on a list of two tensors"
                            ". Got: " + str(X))
                            
        input, theta = X[0], X[1]
        batchSize = input.shape[0]
        theta = theta.reshape((batchSize, 2, 3))
        output = self.transform(theta, input, self.downsampleFactor)

        return output


    def get_output_shape_for(self, inputShapes):
        frameShape = inputShapes[0]
        batchSize = None
        channels = frameShape[1]
        height = int(frameShape[2] / self.downsampleFactor)
        width = int(frameShape[3] / self.downsampleFactor)
        
        return (batchSize, channels, height, width)
        
        
    @staticmethod
    def transform(theta, input, downsample_factor):
        num_batch, num_channels, height, width = input.shape
        theta = T.reshape(theta, (-1, 2, 3))
    
        # grid of (x_t, y_t, 1), eq (1) in ref [1]
        out_height = T.cast(height / downsample_factor, 'int64')
        out_width = T.cast(width / downsample_factor, 'int64')
        grid = SpatialTransformer.meshgrid(out_height, out_width)
    
        # Transform A x (x_t, y_t, 1)^T -> (x_s, y_s)
        T_g = T.dot(theta, grid)
        x_s = T_g[:, 0]
        y_s = T_g[:, 1]
        x_s_flat = x_s.flatten()
        y_s_flat = y_s.flatten()
    
        # dimshuffle input to  (bs, height, width, channels)
        input_dim = input.dimshuffle(0, 2, 3, 1)
        input_transformed = SpatialTransformer.interpolate(
            input_dim, x_s_flat, y_s_flat,
            out_height, out_width)
    
        output = T.reshape(
            input_transformed, (num_batch, out_height, out_width, num_channels))
        output = output.dimshuffle(0, 3, 1, 2)  # dimshuffle to conv format
        
        return output


    @staticmethod
    def meshgrid(height, width):
        # This function is the grid generator from eq. (1) in reference [1].
        # It is equivalent to the following numpy code:
        #  x_t, y_t = np.meshgrid(np.linspace(-1, 1, width),
        #                         np.linspace(-1, 1, height))
        #  ones = np.ones(np.prod(x_t.shape))
        #  grid = np.vstack([x_t.flatten(), y_t.flatten(), ones])
        # It is implemented in Theano instead to support symbolic grid sizes.
        # Note: If the image size is known at layer construction time, we could
        # compute the meshgrid offline in numpy instead of doing it dynamically
        # in Theano. However, it hardly affected performance when we tried.
        x_t = T.dot(T.ones((height, 1)),
                    Data.linspace(-1.0, 1.0, width).dimshuffle('x', 0))
        y_t = T.dot(Data.linspace(-1.0, 1.0, height).dimshuffle(0, 'x'),
                    T.ones((1, width)))
    
        x_t_flat = x_t.reshape((1, -1))
        y_t_flat = y_t.reshape((1, -1))
        ones = T.ones_like(x_t_flat)
        grid = T.concatenate([x_t_flat, y_t_flat, ones], axis=0)
        return grid
        
    
    @staticmethod
    def interpolate(im, x, y, out_height, out_width):
        # *_f are floats
        num_batch, height, width, channels = im.shape
        height_f = T.cast(height, K.floatx())
        width_f = T.cast(width, K.floatx())
    
        # clip coordinates to [-1, 1]
        x = T.clip(x, -1, 1)
        y = T.clip(y, -1, 1)
    
        # scale coordinates from [-1, 1] to [0, width/height - 1]
        x = (x + 1) / 2 * (width_f - 1)
        y = (y + 1) / 2 * (height_f - 1)
    
        # obtain indices of the 2x2 pixel neighborhood surrounding the coordinates;
        # we need those in floatX for interpolation and in int64 for indexing. for
        # indexing, we need to take care they do not extend past the image.
        x0_f = T.floor(x)
        y0_f = T.floor(y)
        x1_f = x0_f + 1
        y1_f = y0_f + 1
        x0 = T.cast(x0_f, 'int64')
        y0 = T.cast(y0_f, 'int64')
        x1 = T.cast(T.minimum(x1_f, width_f - 1), 'int64')
        y1 = T.cast(T.minimum(y1_f, height_f - 1), 'int64')
    
        # The input is [num_batch, height, width, channels]. We do the lookup in
        # the flattened input, i.e [num_batch*height*width, channels]. We need
        # to offset all indices to match the flat version
        dim2 = width
        dim1 = width*height
        base = T.repeat(
            T.arange(num_batch, dtype='int64')*dim1, out_height*out_width)
        base_y0 = base + y0*dim2
        base_y1 = base + y1*dim2
        idx_a = base_y0 + x0
        idx_b = base_y1 + x0
        idx_c = base_y0 + x1
        idx_d = base_y1 + x1
    
        # use indices to lookup pixels for all samples
        im_flat = im.reshape((-1, channels))
        Ia = im_flat[idx_a]
        Ib = im_flat[idx_b]
        Ic = im_flat[idx_c]
        Id = im_flat[idx_d]
    
        # calculate interpolated values
        wa = ((x1_f-x) * (y1_f-y)).dimshuffle(0, 'x')
        wb = ((x1_f-x) * (y-y0_f)).dimshuffle(0, 'x')
        wc = ((x-x0_f) * (y1_f-y)).dimshuffle(0, 'x')
        wd = ((x-x0_f) * (y-y0_f)).dimshuffle(0, 'x')
        output = T.sum([wa*Ia, wb*Ib, wc*Ic, wd*Id], axis=0)
        
        return output