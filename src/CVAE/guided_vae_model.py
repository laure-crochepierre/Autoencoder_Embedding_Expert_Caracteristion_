import os
import json
from matplotlib import pyplot as plt
import numpy as np

from keras.models import Model
from keras.layers import Input, Dense, Lambda, Dropout, BatchNormalization, Activation
from keras.layers.merge import concatenate, Add, Multiply
from keras import backend as K
from keras.callbacks import Callback
from keras import losses
from keras import optimizers
import tensorflow as tf
from functools import partial, update_wrapper


class BaseModel():
    def __init__(self, **kwargs):
        """

        :param kwargs:
        """
        if 'name' not in kwargs:
            raise Exception('Please specify model name!')

        self.name = kwargs['name']

        if 'output' not in kwargs:
            self.output = 'output'
        else:
            self.output = kwargs['output']

        self.trainers = {}
        self.history = None

    def save_model(self, out_dir):
        folder = os.path.join(out_dir)
        if not os.path.isdir(folder):
            os.mkdir(folder)

        for k, v in self.trainers.items():
            filename = os.path.join(folder, '%s.hdf5' % (k))
            v.save_weights(filename)

    def store_to_save(self, name):
        self.trainers[name] = getattr(self, name)

    def load_model(self, folder):
        for k, v in self.trainers.items():
            filename = os.path.join(folder, '%s.hdf5' % (k))
            getattr(self, k).load_weights(filename)

    def main_train(self, dataset, training_epochs=100, batch_size=100, callbacks=[],validation_data=None, verbose=0,validation_split=None):

        out_dir = os.path.join(self.output, self.name)
        if not os.path.isdir(out_dir):
            os.mkdir(out_dir)

        res_out_dir = os.path.join(out_dir, 'results')
        if not os.path.isdir(res_out_dir):
            os.mkdir(res_out_dir)

        wgt_out_dir = os.path.join(out_dir, 'models')
        if not os.path.isdir(wgt_out_dir):
            os.mkdir(wgt_out_dir)

        #if 'test' in dataset.keys():
        #    validation_data = (dataset['test']['x'], dataset['test']['y'])
        #else:
        #    validation_data = None

        print('\n\n--- START TRAINING ---\n')
        history = self.train(dataset['train'], training_epochs, batch_size, callbacks, validation_data=validation_data, verbose=verbose,validation_split=validation_split)

        self.history = history.history
        self.save_model(wgt_out_dir)
        self.plot_loss(res_out_dir)

        with open(os.path.join(res_out_dir, 'history.json'), 'w') as f:
            json.dump(self.history, f)

    def plot_loss(self, path_save = None):

        nb_epoch = len(self.history['loss'])

        if 'val_loss' in self.history.keys():
            best_iter = np.argmin(self.history['val_loss'])
            min_val_loss = self.history['val_loss'][best_iter]

            plt.plot(range(nb_epoch), self.history['val_loss'], label='test (min: {:0.2f}, epch: {:0.2f})'.format(min_val_loss, best_iter))

        plt.plot(range(nb_epoch), self.history['loss'], label = 'train')
        plt.xlabel('epochs')
        plt.ylabel('loss')
        plt.title('loss evolution')
        plt.legend()

        if path_save is not None:
            plt.savefig(os.path.join(path_save, 'loss_evolution.png'))

    #abstractmethod
    def train(self, training_dataset, training_epochs, batch_size, callbacks, validation_data=None, verbose=0,validation_split=None):
        '''
        Plase override "train" method in the derived model!
        '''

        pass
    

#un modèle CVAE ou l'on encode les conditions et on decode le signal
class Guided_VAE(BaseModel):
    def __init__(self, input_dim=[96], cond_dims=[12], z_dim=2, e_dims=[24], d_dims=[24], alpha=1,beta=1, embeddingBeforeLatent=False,pDropout=0.0, verbose=True,is_L2_Loss=True, InfoVAE = False, gamma=0, prior='Gaussian',has_skip=True,has_BN=1, lr = 0.001,**kwargs):
        super().__init__(**kwargs)
        self.input_dim = input_dim
        self.cond_dims = cond_dims
        self.z_dim = z_dim
        self.e_dims = e_dims
        self.d_dims = d_dims
        self.alpha=alpha
        self.beta = beta
        self.dropout = pDropout#la couche de dropout est pour l'instant commentée car pas d'utilité dans les experiences
        self.encoder = None
        self.decoder = None
        self.latent=None
        self.cvae = None
        self.embeddingBeforeLatent=embeddingBeforeLatent#in the decoder, do a skip only with the embedding and not the latent space to make it more influencial
        self.verbose = verbose
        self.losses={}
        self.weight_losses={}
        self.is_L2_Loss=is_L2_Loss
        self.has_skip=has_skip
        self.lr=lr
        self.InfoVAE = InfoVAE
        if self.InfoVAE:
            self.gamma= gamma
        self.has_BN=has_BN
        self.prior = prior

        self.build_model()

    def build_model(self):
        """

        :param verbose:
        :return:
        """

        self.encoder = self.build_encoder()
        self.decoder = self.build_decoder()
        if self.InfoVAE:
            print('InfoVAE : ', str(self.InfoVAE))

        x_true = Input(shape=(self.input_dim[0],), name='x_true')
        cond_true = [Input(shape=(cond_dim,)) for cond_dim in self.input_dim[1:]]
        x_inputs = [x_true] + cond_true
        # Encoding
        z_mu, z_log_sigma = self.encoder(x_inputs)
        #self.latent=Lambda(lambda x:x,'latent')(z_mu)
        
        x_true_inputs = Input(shape=(self.input_dim[0],), name='x_true_zmu_Layer') 
        x = Lambda(lambda x: x,name='z_mu')(x_true_inputs)
        self.latent=Model(inputs=[x_true_inputs], outputs=[x], name='z_mu_output')
        ZMU=self.latent(z_mu)



        # Sampling
        # Here if the prior is Gaussian, z_log_sigma is the log of sigma², whereas it refers to the log of sigma if it's Laplacian. 
        def sample_z(args):
            mu, log_sigma = args
            if self.prior=='Gaussian':
                eps = K.random_normal(shape=(K.shape(mu)[0], self.z_dim), mean=0., stddev=1.)
                return mu + K.exp(log_sigma / 2) * eps
            elif self.prior=='Laplace':
                U = K.random_uniform(shape=(K.shape(mu)[0], self.z_dim), minval =0.0, maxval=1.)
                V = K.random_uniform(shape=(K.shape(mu)[0], self.z_dim), minval =0.0, maxval=1.)
                Rad_sample = 2.*K.cast(K.greater_equal(V,0.5), dtype='float32') - 1. 
                Expon_sample = -K.exp(log_sigma)*K.log(1-U)
                return mu + Rad_sample*Expon_sample

        z = Lambda(sample_z, name='sample_z')([z_mu, z_log_sigma])

        # Decoding
        x_hat= self.decoder(z)
        
        #identity layer to have two output layers and compute separately 2 losses (the kl and the reconstruction)
             
        x = Lambda(lambda x: x)(x_true_inputs)
        identitModel=Model(inputs=[x_true_inputs], outputs=[x], name='decoder_for_kl')
        if self.InfoVAE :
            identitModel2=Model(inputs=[x_true_inputs], outputs=[x], name='decoder_info')
            xhatTer = identitModel2(x_hat)

        
        xhatBis=identitModel(x_hat)

        # Defining loss
        if self.InfoVAE:
            # Defining loss
            vae_loss, recon_loss, kl_loss, info_loss= self.build_loss_info(z_mu, z_log_sigma, z, beta=self.beta, gamma=self.gamma)

            # Defining and compiling cvae model
            self.losses = {"decoder": recon_loss,"decoder_for_kl": kl_loss, "decoder_info": info_loss}
            #lossWeights = {"decoder": 1.0, "decoder_for_kl": 0.01}
            self.weight_losses = {"decoder": self.alpha, "decoder_for_kl": self.beta, "decoder_info":self.gamma}

            Opt_Adam = optimizers.Adam(lr=self.lr)
            
            self.cvae = Model(inputs=x_inputs, outputs=[x_hat,xhatBis, xhatTer])#self.encoder.outputs])
            self.cvae.compile(optimizer=Opt_Adam,loss=self.losses,loss_weights=self.weight_losses)
            
        else:
            vae_loss, recon_loss, kl_loss = self.build_loss(z_mu, z_log_sigma, beta=self.beta)

            # Defining and compiling cvae model
            self.losses = {"decoder": recon_loss,"decoder_for_kl": kl_loss}
            #lossWeights = {"decoder": 1.0, "decoder_for_kl": 0.01}
            self.weight_losses = {"decoder": self.alpha, "decoder_for_kl": self.beta}

            Opt_Adam = optimizers.Adam(lr=self.lr)
            
            self.cvae = Model(inputs=x_inputs, outputs=[x_hat,xhatBis])#self.encoder.outputs])
            self.cvae.compile(optimizer=Opt_Adam,loss=self.losses,loss_weights=self.weight_losses)
            
            
        # Store trainers
        self.store_to_save('cvae')
        self.store_to_save('encoder')
        self.store_to_save('decoder')

        if self.verbose:
            print("complete model: ")
            self.cvae.summary()
            print("encoder: ")
            self.encoder.summary()
            print("decoder: ")
            self.decoder.summary()

    def build_encoder(self):
        """
        Encoder: Q(z|X,y)
        :return:
        """
        x_inputs = []
        mu_cond=[]

        for i,input_dim in enumerate(self.input_dim):
            if i==0:
                x = Input(shape=(input_dim,), name='enc_x_true')
                x_inputs.append(x)

                nLayers = len(self.e_dims)
                for idx, layer_dim in enumerate(self.e_dims):
                    #x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
                    x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
                    #x = Dropout(self.dropout)(x)

                #z_mu = Dense(units=self.z_dim, activation='linear', name="latent_dense_mu")(x)
                #z_log_sigma = Dense(units=self.z_dim, activation='linear', name='latent_dense_log_sigma')(x)
                #x = Dense(units=self.z_dim, activation='relu', name="enc_dense_zdim")(x)
                z_mu = Dense(units=self.z_dim, activation='linear', name="latent_dense_mu")(x)
                z_log_sigma = Dense(units=self.z_dim, activation='linear', name='latent_dense_log_sigma')(x)

                z_mu = Lambda(lambda z: z*0, name='neutralize_signal')(z_mu)

            else :
                x = Input(shape=(input_dim,), name='enc_cond_{}'.format(i))
                x_inputs.append(x)
                class_cond = Lambda(lambda x: K.one_hot(K.constant(i,dtype='int32',shape=(1,)), len(self.cond_dims)), name="latent_dense_mu_c_class_{}".format(i))(x)

                nLayers = len(self.cond_dims[i-1])
                for idx, layer_dim in enumerate(self.cond_dims[i-1]):
                    #x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
                    x = Dense(units=layer_dim, activation='relu', name="enc_cond_{}_dense_{}".format(i,idx))(x)
                    #x = Dropout(self.dropout)(x)

                #z_mu = Dense(units=self.z_dim, activation='linear', name="latent_dense_mu")(x)
                #z_log_sigma = Dense(units=self.z_dim, activation='linear', name='latent_dense_log_sigma')(x)
                #x = Dense(units=self.z_dim, activation='relu', name="enc_dense_zdim")(x)


                z_mu_c = Dense(units=self.z_dim, activation='linear', name="latent_dense_mu_c_{}".format(i))(x)
                #class_cond = Dense(units=len(self.cond_dims), activation='softmax', name="latent_dense_mu_c_class_{}".format(i))(x)

                dim_cond = Dense(units=self.z_dim, activation='hard_sigmoid', name= "latent_dense_mu_c_dim_{}".format(i))(class_cond)

                z_mu_cond = Multiply()([z_mu_c, dim_cond])

                mu_cond.append(z_mu_cond)

        z_mu = Add()([z_mu] + mu_cond)


        return Model(inputs=x_inputs, outputs=[z_mu, z_log_sigma], name='encoder')

    def build_decoder(self):
        """
        Decoder: P(X|z,y)
        :return:
        """

        x_inputs = Input(shape=(self.z_dim,), name='dec_z')
        x = x_inputs
        nLayers=len(self.d_dims)
        for idx, layer_dim in reversed(list(enumerate(self.d_dims))):
            #x = Dense(units=layer_dim, activation='relu', name='dec_dense_{}'.format(idx))(x)
            if(idx==0):
                x = concatenate([Dense(units=layer_dim, activation='relu')(x), x],name="dec_dense_resnet{}".format(idx)) 
                #x = Dense(units=layer_dim, activation='relu',name="dec_dense_resnet{}".format(idx))(x) 
            else:
                if (idx==0 and self.embeddingBeforeLatent):#we make the embedding more influential
                    #x = concatenate([Dense(units=layer_dim, activation='relu')(x), cond_inputs], name="enc_dense_resnet{}".format(idx)) #plus rapide dans l'apprentissage mais sans doute moins scalable..!
                    print('cool')
                    x = concatenate([Dense(units=layer_dim, activation='relu')(x), x],name="dec_dense_resnet{}".format(idx)) 

                else:
                    #x = Dense(units=layer_dim, activation='relu',name="dec_dense_resnet{}".format(idx))(x) 
                    if(self.has_skip):
                        x = concatenate([Dense(units=layer_dim, activation='relu')(x), x],name="dec_dense_resnet{}".format(idx)) 
                    else:
                        x = Dense(units=layer_dim, activation='relu',name="dec_dense_resnet{}".format(idx))(x) 
            #x = Dropout(self.dropout)(x)
        #xprevious=x
        output = Dense(units=self.input_dim[0], activation='linear', name='dec_x_hat')(x)
        #outputBis = Lambda(lambda x: x)(x)

        return Model(inputs=x_inputs, outputs=output, name='decoder')

    def build_loss(self, z_mu, z_log_sigma,beta=0):
        """

        :return:
        """

        def kl_loss(y_true, y_pred):
            if self.prior == 'Gaussian':
                return 0.5 * K.sum(K.exp(z_log_sigma) + K.square(z_mu) - 1. - z_log_sigma, axis=-1)
            elif self.prior == 'Laplace':
                return K.sum(K.abs(z_mu) + K.exp(z_log_sigma)*K.exp(-K.abs(z_mu)/K.exp(z_log_sigma)) - 1. - z_log_sigma, axis=-1)


        def recon_loss(y_true, y_pred):
            if(self.is_L2_Loss):
                print("L2 loss")
                print(self.is_L2_Loss)
                return K.sum(K.square(y_pred - y_true), axis=-1)
            else:
                print("L1 loss")
                print(self.is_L2_Loss)
                return K.sum(K.abs(y_pred - y_true), axis=-1)

        def vae_loss(y_true, y_pred, beta=0, gamma=0):
            """ Calculate loss = reconstruction loss + KL loss for each data in minibatch """

            # E[log P(X|z,y)]
            recon = recon_loss(y_true=y_true, y_pred=y_pred)

            # D_KL(Q(z|X,y) || P(z|X)); calculate in closed form as both dist. are Gaussian
            kl = kl_loss(y_true=y_true, y_pred=y_pred)

            return recon + beta*kl

        return vae_loss, recon_loss, kl_loss
    
    def build_loss_info(self, z_mu, z_log_sigma, z,beta=0.5, gamma=1):
        """

        :return:
        """

        def kl_loss(y_true, y_pred):
            if self.prior == 'Gaussian':
                return 0.5 * K.sum(K.exp(z_log_sigma) + K.square(z_mu) - 1. - z_log_sigma, axis=-1)
            elif self.prior == 'Laplace':
                return K.sum(K.abs(z_mu) + K.exp(z_log_sigma)*K.exp(-K.abs(z_mu)/K.exp(z_log_sigma)) - 1. - z_log_sigma, axis=-1)


        def recon_loss(y_true, y_pred):
            if(self.is_L2_Loss):
                print("L2 loss")
                print(self.is_L2_Loss)
                return K.sum(K.square(y_pred - y_true), axis=-1)
            else:
                print("L1 loss")
                print(self.is_L2_Loss)
                return K.sum(K.abs(y_pred - y_true), axis=-1)

        def kde(s1,s2,h=None):
            dim = K.shape(s1)[1]
            s1_size = K.shape(s1)[0]
            s2_size = K.shape(s2)[0]
            if h is None:
                h = K.cast(dim, dtype='float32') / 2
            tiled_s1 = K.tile(K.reshape(s1, K.stack([s1_size, 1, dim])), K.stack([1, s2_size, 1]))
            tiled_s2 = K.tile(K.reshape(s2, K.stack([1, s2_size, dim])), K.stack([s1_size, 1, 1]))
            return K.exp(-0.5 * K.sum(K.square(tiled_s1 - tiled_s2), axis=-1)  / h)

        def info_loss(y_true, y_pred):
            q_kernel = kde(z_mu, z_mu)
            p_kernel = kde(z, z)
            pq_kernel = kde(z_mu, z)
            return K.mean(q_kernel) + K.mean(p_kernel) - 2 * K.mean(pq_kernel)

        def vae_loss(y_true, y_pred, beta=0, gamma=0):
            """ Calculate loss = reconstruction loss + KL loss for each data in minibatch """

            # E[log P(X|z,y)]
            recon = recon_loss(y_true=y_true, y_pred=y_pred)

            # D_KL(Q(z|X,y) || P(z|X)); calculate in closed form as both dist. are Gaussian
            kl = kl_loss(y_true=y_true, y_pred=y_pred)

            #D(q(z)|| p(z)); calculated with the MMD estimator using a Gaussian kernel
            info = info_loss(y_true=y_true, y_pred=y_pred)

            return recon + beta*kl + gamma*info

        return vae_loss, recon_loss, kl_loss, info_loss


    def train(self, dataset_train, training_epochs=10, batch_size=20, callbacks = [], validation_data = None, verbose = True,validation_split=None):
        """

        :param dataset_train:
        :param training_epochs:
        :param batch_size:
        :param callbacks:
        :param validation_data:
        :param verbose:
        :return:
        """

        assert len(dataset_train) >= 2  # Check that both x and cond are present
        #outputs=np.array([dataset_train['y'],dataset_train['y1']])
        output1=dataset_train['y']
        output2=dataset_train['y']

        if self.InfoVAE:
            output3=dataset_train['y']

            cvae_hist = self.cvae.fit(dataset_train['x'], [output1,output2,output3], batch_size=batch_size, epochs=training_epochs,
                                 validation_data=validation_data,validation_split=validation_split,
                                 callbacks=callbacks, verbose=verbose)
        else:
            cvae_hist = self.cvae.fit(dataset_train['x'], [output1,output2], batch_size=batch_size, epochs=training_epochs,
                                 validation_data=validation_data,validation_split=validation_split,
                                 callbacks=callbacks, verbose=verbose)

        return cvae_hist


#Un model VAE où un block LEAP est ajouté a l'aval de l'encoding. 
class LEAP_VAE(BaseModel):
    def __init__(self, input_dim=96, cond_dims=12, z_dim=2, e_dims=[24], d_dims=[24], e_leap_dims=[3], beta=1, embeddingBeforeLatent=False,pDropout=0.0, verbose=True,is_L2_Loss=True, InfoVAE = False, gamma=0, prior='Gaussian',anisotropic_prior=None,has_skip=True,has_BN=1, lr = 0.001,**kwargs):
        super().__init__(**kwargs)
        self.input_dim = input_dim
        self.cond_dims = cond_dims
        self.z_dim = z_dim
        self.e_dims = e_dims
        self.d_dims = d_dims
        self.e_leap_dims = e_leap_dims
        self.beta = beta
        self.dropout = pDropout#la couche de dropout est pour l'instant commentée car pas d'utilité dans les experiences
        self.encoder = None
        self.decoder = None
        self.latent=None
        self.cvae = None
        self.embeddingBeforeLatent=embeddingBeforeLatent#in the decoder, do a skip only with the embedding and not the latent space to make it more influencial
        self.verbose = verbose
        self.losses={}
        self.weight_losses={}
        self.is_L2_Loss=is_L2_Loss
        self.has_skip=has_skip
        self.lr=lr
        self.InfoVAE = InfoVAE
        if self.InfoVAE:
            self.gamma= gamma
        self.has_BN=has_BN
        self.prior = prior
        self.anisotropic_prior = anisotropic_prior

        self.build_model()

    def build_model(self):
        """

        :param verbose:
        :return:
        """

        self.encoder = self.build_encoder()
        self.decoder = self.build_decoder()
        if self.InfoVAE:
            print('InfoVAE : ', str(self.InfoVAE))

        x_true = Input(shape=(self.input_dim,), name='x_true')
        cond_true = Input(shape=(self.cond_dims,), name='cond_true')
        x_inputs = [x_true,cond_true]
        # Encoding
        z_mu, z_log_sigma = self.encoder(x_inputs)
        #self.latent=Lambda(lambda x:x,'latent')(z_mu)
        
        x_true_inputs = Input(shape=(self.input_dim,), name='x_true_zmu_Layer') 
        x = Lambda(lambda x: x,name='z_mu')(x_true_inputs)
        self.latent=Model(inputs=[x_true_inputs], outputs=[x], name='z_mu_output')
        ZMU=self.latent(z_mu)



        # Sampling
        # Here if the prior is Gaussian, z_log_sigma is the log of sigma², whereas it refers to the log of sigma if it's Laplacian. 
        def sample_z(args):
            mu, log_sigma = args
            if self.prior=='Gaussian':
                eps = K.random_normal(shape=(K.shape(mu)[0], self.z_dim), mean=0., stddev=1.)
                return mu + K.exp(log_sigma / 2) * eps
            elif self.prior=='Laplace':
                U = K.random_uniform(shape=(K.shape(mu)[0], self.z_dim), minval =0.0, maxval=1.)
                V = K.random_uniform(shape=(K.shape(mu)[0], self.z_dim), minval =0.0, maxval=1.)
                Rad_sample = 2.*K.cast(K.greater_equal(V,0.5), dtype='float32') - 1. 
                Expon_sample = -K.exp(log_sigma)*K.log(1-U)
                return mu + Rad_sample*Expon_sample

        z = Lambda(sample_z, name='sample_z')([z_mu, z_log_sigma])

        # Decoding
        x_hat= self.decoder(z)
        
        #identity layer to have two output layers and compute separately 2 losses (the kl and the reconstruction)
             
        x = Lambda(lambda x: x)(x_true_inputs)
        identitModel=Model(inputs=[x_true_inputs], outputs=[x], name='decoder_for_kl')
        if self.InfoVAE :
            identitModel2=Model(inputs=[x_true_inputs], outputs=[x], name='decoder_info')
            xhatTer = identitModel2(x_hat)

        
        xhatBis=identitModel(x_hat)

        # Defining loss
        if self.InfoVAE:
            # Defining loss
            vae_loss, recon_loss, kl_loss, info_loss= self.build_loss_info(z_mu, z_log_sigma, z, beta=self.beta, gamma=self.gamma)

            # Defining and compiling cvae model
            self.losses = {"decoder": recon_loss,"decoder_for_kl": kl_loss, "decoder_info": info_loss}
            #lossWeights = {"decoder": 1.0, "decoder_for_kl": 0.01}
            self.weight_losses = {"decoder": 2.0, "decoder_for_kl": self.beta, "decoder_info":self.gamma}

            Opt_Adam = optimizers.Adam(lr=self.lr)
            
            self.cvae = Model(inputs=x_inputs, outputs=[x_hat,xhatBis, xhatTer])#self.encoder.outputs])
            self.cvae.compile(optimizer=Opt_Adam,loss=self.losses,loss_weights=self.weight_losses)
            
        else:
            vae_loss, recon_loss, kl_loss = self.build_loss(z_mu, z_log_sigma, beta=self.beta)

            # Defining and compiling cvae model
            self.losses = {"decoder": recon_loss,"decoder_for_kl": kl_loss}
            #lossWeights = {"decoder": 1.0, "decoder_for_kl": 0.01}
            self.weight_losses = {"decoder": 2.0, "decoder_for_kl": self.beta}

            Opt_Adam = optimizers.Adam(lr=self.lr)
            
            self.cvae = Model(inputs=x_inputs, outputs=[x_hat,xhatBis])#self.encoder.outputs])
            self.cvae.compile(optimizer=Opt_Adam,loss=self.losses,loss_weights=self.weight_losses)
            
            
        # Store trainers
        self.store_to_save('cvae')
        self.store_to_save('encoder')
        self.store_to_save('decoder')

        if self.verbose:
            print("complete model: ")
            self.cvae.summary()
            print("encoder: ")
            self.encoder.summary()
            print("decoder: ")
            self.decoder.summary()

    def build_encoder(self):
        """
        Encoder: Q(z|X,y)
        :return:
        """
        x_inputs = []

        self.leap_block = self.build_leap_block()

        x = Input(shape=(self.input_dim,), name='enc_x_true')
        x_inputs.append(x)

        cond_inputs = Input(shape=(self.cond_dims,), name='enc_cond')
        x_inputs.append(cond_inputs)

        nLayers = len(self.e_dims)
        for idx, layer_dim in enumerate(self.e_dims):
            #x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
            x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
            #x = Dropout(self.dropout)(x)

        #z_mu = Dense(units=self.z_dim, activation='linear', name="latent_dense_mu")(x)
        #z_log_sigma = Dense(units=self.z_dim, activation='linear', name='latent_dense_log_sigma')(x)
        #x = Dense(units=self.z_dim, activation='relu', name="enc_dense_zdim")(x)
        z_mu = Dense(units=self.z_dim, activation='linear', name="latent_dense_mu")(x)
        z_log_sigma = Dense(units=self.z_dim, activation='linear', name='latent_dense_log_sigma')(x)

        leap_cond = self.leap_block([z_mu, cond_inputs])

        z_mu = Add()([z_mu,leap_cond])


        return Model(inputs=x_inputs, outputs=[z_mu, z_log_sigma], name='encoder')

    def build_decoder(self):
        """
        Decoder: P(X|z,y)
        :return:
        """

        x_inputs = Input(shape=(self.z_dim,), name='dec_z')
        x = x_inputs
        nLayers=len(self.d_dims)
        for idx, layer_dim in reversed(list(enumerate(self.d_dims))):
            #x = Dense(units=layer_dim, activation='relu', name='dec_dense_{}'.format(idx))(x)
            if(idx==0):
                x = concatenate([Dense(units=layer_dim, activation='relu')(x), x],name="dec_dense_resnet{}".format(idx)) 
                #x = Dense(units=layer_dim, activation='relu',name="dec_dense_resnet{}".format(idx))(x) 
            else:
                if (idx==0 and self.embeddingBeforeLatent):#we make the embedding more influential
                    #x = concatenate([Dense(units=layer_dim, activation='relu')(x), cond_inputs], name="enc_dense_resnet{}".format(idx)) #plus rapide dans l'apprentissage mais sans doute moins scalable..!
                    print('cool')
                    x = concatenate([Dense(units=layer_dim, activation='relu')(x), x],name="dec_dense_resnet{}".format(idx)) 

                else:
                    #x = Dense(units=layer_dim, activation='relu',name="dec_dense_resnet{}".format(idx))(x) 
                    if(self.has_skip):
                        x = concatenate([Dense(units=layer_dim, activation='relu')(x), x],name="dec_dense_resnet{}".format(idx)) 
                    else:
                        x = Dense(units=layer_dim, activation='relu',name="dec_dense_resnet{}".format(idx))(x) 
            #x = Dropout(self.dropout)(x)
        #xprevious=x
        output = Dense(units=self.input_dim, activation='linear', name='dec_x_hat')(x)
        #outputBis = Lambda(lambda x: x)(x)

        return Model(inputs=x_inputs, outputs=output, name='decoder')

    def build_leap_block(self):

        cond_inputs = Input(shape=(self.cond_dims,), name='leap_enc_cond')

        x= Input(shape=(self.z_dim,), name='leap_enc_inputs')
        
        leap_inputs = [x,cond_inputs]

        Layers = len(self.e_leap_dims)
        for idx, layer_dim in enumerate(self.e_leap_dims):
            #x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
            x = Dense(units=layer_dim, activation='relu', name="leap_enc_dense_{}".format(idx))(x)
            #x = Dropout(self.dropout)(x)

        #z_mu = Dense(units=self.z_dim, activation='linear', name="latent_dense_mu")(x)
        #z_log_sigma = Dense(units=self.z_dim, activation='linear', name='latent_dense_log_sigma')(x)
        #x = Dense(units=self.z_dim, activation='relu', name="enc_dense_zdim")(x)
        tau_leap = Dense(units=self.cond_dims, use_bias=False, activation='relu', name="leap_dense_latent")(x)

        leap_product = Multiply()([tau_leap, cond_inputs])

        for idx, layer_dim in reversed(list(enumerate(self.e_leap_dims))):
            #x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
            leap_product = Dense(units=layer_dim, activation='relu', name="leap_dec_dense_{}".format(idx))(leap_product)

        leap_output = Dense(units=self.z_dim, activation='linear', name="latent_dense_leap")(leap_product)

        return Model(inputs=leap_inputs, outputs=leap_output, name='leap_block')

    def build_loss(self, z_mu, z_log_sigma,beta=0):
        """

        :return:
        """

        def kl_loss(y_true, y_pred):
            if self.anisotropic_prior is None:
                if self.prior == 'Gaussian':
                    return 0.5 * K.sum(K.exp(z_log_sigma) + K.square(z_mu) - 1. - z_log_sigma, axis=-1)
                elif self.prior == 'Laplace':
                    return K.sum(K.abs(z_mu) + K.exp(z_log_sigma)*K.exp(-K.abs(z_mu)/K.exp(z_log_sigma)) - 1. - z_log_sigma, axis=-1)
            else:
                if self.prior == 'Gaussian':
                    return 0.5 * K.sum(self.anisotropic_prior + (K.exp(z_log_sigma) + K.square(z_mu)) / K.exp(self.anisotropic_prior) - 1. - z_log_sigma, axis=-1)
                
                elif self.prior == 'Laplace':
                    return K.sum((K.abs(z_mu) + K.exp(z_log_sigma)*K.exp(-K.abs(z_mu)/K.exp(z_log_sigma))) / K.exp(self.anisotropic_prior) - 1. - z_log_sigma + self.anisotropic_prior, axis=-1)

        def recon_loss(y_true, y_pred):
            if(self.is_L2_Loss):
                print("L2 loss")
                print(self.is_L2_Loss)
                return K.sum(K.square(y_pred - y_true), axis=-1)
            else:
                print("L1 loss")
                print(self.is_L2_Loss)
                return K.sum(K.abs(y_pred - y_true), axis=-1)

        def vae_loss(y_true, y_pred, beta=0, gamma=0):
            """ Calculate loss = reconstruction loss + KL loss for each data in minibatch """

            # E[log P(X|z,y)]
            recon = recon_loss(y_true=y_true, y_pred=y_pred)

            # D_KL(Q(z|X,y) || P(z|X)); calculate in closed form as both dist. are Gaussian
            kl = kl_loss(y_true=y_true, y_pred=y_pred)

            return recon + beta*kl

        return vae_loss, recon_loss, kl_loss
    
    def build_loss_info(self, z_mu, z_log_sigma, z,beta=0.5, gamma=1):
        """

        :return:
        """

        def kl_loss(y_true, y_pred):
            if self.prior == 'Gaussian':
                return 0.5 * K.sum(K.exp(z_log_sigma) + K.square(z_mu) - 1. - z_log_sigma, axis=-1)
            elif self.prior == 'Laplace':
                return K.sum(K.abs(z_mu) + K.exp(z_log_sigma)*K.exp(-K.abs(z_mu)/K.exp(z_log_sigma)) - 1. - z_log_sigma, axis=-1)


        def recon_loss(y_true, y_pred):
            if(self.is_L2_Loss):
                print("L2 loss")
                print(self.is_L2_Loss)
                return K.sum(K.square(y_pred - y_true), axis=-1)
            else:
                print("L1 loss")
                print(self.is_L2_Loss)
                return K.sum(K.abs(y_pred - y_true), axis=-1)

        def kde(s1,s2,h=None):
            dim = K.shape(s1)[1]
            s1_size = K.shape(s1)[0]
            s2_size = K.shape(s2)[0]
            if h is None:
                h = K.cast(dim, dtype='float32') / 2
            tiled_s1 = K.tile(K.reshape(s1, K.stack([s1_size, 1, dim])), K.stack([1, s2_size, 1]))
            tiled_s2 = K.tile(K.reshape(s2, K.stack([1, s2_size, dim])), K.stack([s1_size, 1, 1]))
            return K.exp(-0.5 * K.sum(K.square(tiled_s1 - tiled_s2), axis=-1)  / h)

        def info_loss(y_true, y_pred):
            q_kernel = kde(z_mu, z_mu)
            p_kernel = kde(z, z)
            pq_kernel = kde(z_mu, z)
            return K.mean(q_kernel) + K.mean(p_kernel) - 2 * K.mean(pq_kernel)

        def vae_loss(y_true, y_pred, beta=0, gamma=0):
            """ Calculate loss = reconstruction loss + KL loss for each data in minibatch """

            # E[log P(X|z,y)]
            recon = recon_loss(y_true=y_true, y_pred=y_pred)

            # D_KL(Q(z|X,y) || P(z|X)); calculate in closed form as both dist. are Gaussian
            kl = kl_loss(y_true=y_true, y_pred=y_pred)

            #D(q(z)|| p(z)); calculated with the MMD estimator using a Gaussian kernel
            info = info_loss(y_true=y_true, y_pred=y_pred)

            return recon + beta*kl + gamma*info

        return vae_loss, recon_loss, kl_loss, info_loss


    def train(self, dataset_train, training_epochs=10, batch_size=20, callbacks = [], validation_data = None, verbose = True,validation_split=None):
        """

        :param dataset_train:
        :param training_epochs:
        :param batch_size:
        :param callbacks:
        :param validation_data:
        :param verbose:
        :return:
        """

        assert len(dataset_train) >= 2  # Check that both x and cond are present
        #outputs=np.array([dataset_train['y'],dataset_train['y1']])
        output1=dataset_train['y']
        output2=dataset_train['y']

        if self.InfoVAE:
            output3=dataset_train['y']

            cvae_hist = self.cvae.fit(dataset_train['x'], [output1,output2,output3], batch_size=batch_size, epochs=training_epochs,
                                 validation_data=validation_data,validation_split=validation_split,
                                 callbacks=callbacks, verbose=verbose)
        else:
            cvae_hist = self.cvae.fit(dataset_train['x'], [output1,output2], batch_size=batch_size, epochs=training_epochs,
                                 validation_data=validation_data,validation_split=validation_split,
                                 callbacks=callbacks, verbose=verbose)

        return cvae_hist

#Un model CVAE où un block LEAP est ajouté a l'aval de l'encoding. 
class LEAP_CVAE(BaseModel):
    def __init__(self, input_dim=96, tau_dims=12,cond_dims=12, z_dim=2, e_dims=[24], d_dims=[24], e_leap_dims=[3],alpha=1, beta=1, embeddingBeforeLatent=False,pDropout=0.0, verbose=True,is_L2_Loss=True, InfoVAE = False, gamma=0, pdf_model='Gaussian',anisotropic_prior=None,has_skip=True,has_BN=1, lr = 0.001,**kwargs):
        super().__init__(**kwargs)
        self.input_dim = input_dim
        self.cond_dims = cond_dims
        self.tau_dims = tau_dims
        self.z_dim = z_dim
        self.e_dims = e_dims
        self.d_dims = d_dims
        self.e_leap_dims = e_leap_dims
        self.alpha=alpha
        self.beta = beta
        self.dropout = pDropout#la couche de dropout est pour l'instant commentée car pas d'utilité dans les experiences
        self.encoder = None
        self.decoder = None
        self.latent=None
        self.cvae = None
        self.embeddingBeforeLatent=embeddingBeforeLatent#in the decoder, do a skip only with the embedding and not the latent space to make it more influencial
        self.verbose = verbose
        self.losses={}
        self.weight_losses={}
        self.is_L2_Loss=is_L2_Loss
        self.has_skip=has_skip
        self.lr=lr
        self.InfoVAE = InfoVAE
        if self.InfoVAE:
            self.gamma= gamma
        self.has_BN=has_BN
        self.pdf_model = pdf_model
        self.anisotropic_prior = anisotropic_prior

        self.build_model()

    def build_model(self):
        """

        :param verbose:
        :return:
        """

        self.encoder = self.build_encoder()
        self.decoder = self.build_decoder()
        if self.InfoVAE:
            print('InfoVAE : ', str(self.InfoVAE))

        x_true = Input(shape=(self.input_dim,), name='x_true')
        tau_true = Input(shape=(self.tau_dims,), name='tau_true')
        cond_true = Input(shape=(self.cond_dims,), name='cond_true')
        x_inputs = [x_true,tau_true,cond_true]
        # Encoding
        z_mu, z_log_sigma = self.encoder(x_inputs)
        #self.latent=Lambda(lambda x:x,'latent')(z_mu)
        
        x_true_inputs = Input(shape=(self.input_dim,), name='x_true_zmu_Layer') 
        x = Lambda(lambda x: x,name='z_mu')(x_true_inputs)
        self.latent=Model(inputs=[x_true_inputs], outputs=[x], name='z_mu_output')
        ZMU=self.latent(z_mu)



        # Sampling
        # Here if the prior is Gaussian, z_log_sigma is the log of sigma², whereas it refers to the log of sigma if it's Laplacian. 
        def sample_z(args):
            mu, log_sigma = args
            if self.pdf_model=='Gaussian':
                eps = K.random_normal(shape=(K.shape(mu)[0], self.z_dim), mean=0., stddev=1.)
                return mu + K.exp(log_sigma / 2) * eps
            elif self.pdf_model=='Laplace':
                U = K.random_uniform(shape=(K.shape(mu)[0], self.z_dim), minval =0.0, maxval=1.)
                V = K.random_uniform(shape=(K.shape(mu)[0], self.z_dim), minval =0.0, maxval=1.)
                Rad_sample = 2.*K.cast(K.greater_equal(V,0.5), dtype='float32') - 1. 
                Expon_sample = -K.exp(log_sigma)*K.log(1-U)
                return mu + Rad_sample*Expon_sample

        z = Lambda(sample_z, name='sample_z')([z_mu, z_log_sigma])

        # Decoding
        x_hat= self.decoder([z,cond_true])
        
        #identity layer to have two output layers and compute separately 2 losses (the kl and the reconstruction)
             
        x = Lambda(lambda x: x)(x_true_inputs)
        identitModel=Model(inputs=[x_true_inputs], outputs=[x], name='decoder_for_kl')
        if self.InfoVAE :
            identitModel2=Model(inputs=[x_true_inputs], outputs=[x], name='decoder_info')
            xhatTer = identitModel2(x_hat)

        
        xhatBis=identitModel(x_hat)

        # Defining loss
        if self.InfoVAE:
            # Defining loss
            vae_loss, recon_loss, kl_loss, info_loss= self.build_loss_info(z_mu, z_log_sigma, z, beta=self.beta, gamma=self.gamma)

            # Defining and compiling cvae model
            self.losses = {"decoder": recon_loss,"decoder_for_kl": kl_loss, "decoder_info": info_loss}
            #lossWeights = {"decoder": 1.0, "decoder_for_kl": 0.01}
            self.weight_losses = {"decoder": self.alpha, "decoder_for_kl": self.beta, "decoder_info":self.gamma}

            Opt_Adam = optimizers.Adam(lr=self.lr)
            
            self.cvae = Model(inputs=x_inputs, outputs=[x_hat,xhatBis, xhatTer])#self.encoder.outputs])
            self.cvae.compile(optimizer=Opt_Adam,loss=self.losses,loss_weights=self.weight_losses)
            
        else:
            vae_loss, recon_loss, kl_loss = self.build_loss(z_mu, z_log_sigma, beta=self.beta)

            # Defining and compiling cvae model
            self.losses = {"decoder": recon_loss,"decoder_for_kl": kl_loss}
            #lossWeights = {"decoder": 1.0, "decoder_for_kl": 0.01}
            self.weight_losses = {"decoder": self.alpha, "decoder_for_kl": self.beta}

            Opt_Adam = optimizers.Adam(lr=self.lr)
            
            self.cvae = Model(inputs=x_inputs, outputs=[x_hat,xhatBis])#self.encoder.outputs])
            self.cvae.compile(optimizer=Opt_Adam,loss=self.losses,loss_weights=self.weight_losses)
            
            
        # Store trainers
        self.store_to_save('cvae')
        self.store_to_save('encoder')
        self.store_to_save('decoder')

        if self.verbose:
            print("complete model: ")
            self.cvae.summary()
            print("encoder: ")
            self.encoder.summary()
            print("decoder: ")
            self.decoder.summary()

    def build_encoder(self):
        """
        Encoder: Q(z|X,y)
        :return:
        """
        x_inputs = []

        self.leap_block = self.build_leap_block()

        x_true = Input(shape=(self.input_dim,), name='enc_x_true')
        x_inputs.append(x_true)
        tau_inputs = Input(shape=(self.tau_dims,), name='enc_tau')
        if(self.cond_dims>=1):
            cond_inputs = Input(shape=(self.cond_dims,), name='enc_cond')
        else:
            cond_inputs = Input(shape=(0,), name='enc_cond')

        x = concatenate([x_true, cond_inputs], name='enc_input')
        x_inputs.append(tau_inputs)
        x_inputs.append(cond_inputs)

        nLayers = len(self.e_dims)
        for idx, layer_dim in enumerate(self.e_dims):
            #x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
            if (idx<(nLayers-1)):
                x = concatenate([Dense(units=layer_dim, activation='relu')(x), cond_inputs], name="enc_dense_{}".format(idx))
            else:
                x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
            #x = Dropout(self.dropout)(x)

        #z_mu = Dense(units=self.z_dim, activation='linear', name="latent_dense_mu")(x)
        #z_log_sigma = Dense(units=self.z_dim, activation='linear', name='latent_dense_log_sigma')(x)
        #x = Dense(units=self.z_dim, activation='relu', name="enc_dense_zdim")(x)
        z_mu = Dense(units=self.z_dim, activation='linear', name="latent_dense_mu")(x)
        z_log_sigma = Dense(units=self.z_dim, activation='linear', name='latent_dense_log_sigma')(x)

        leap_cond = self.leap_block([z_mu, tau_inputs])

        z_mu = Add()([z_mu,leap_cond])


        return Model(inputs=x_inputs, outputs=[z_mu, z_log_sigma], name='encoder')

    def build_decoder(self):
        """
        Decoder: P(X|z,y)
        :return:
        """

        x_inputs = Input(shape=(self.z_dim,), name='dec_z')

        if(self.cond_dims>=1):
            cond_inputs = Input(shape=(self.cond_dims,), name='dec_cond')
        else:
            cond_inputs = Input(shape=(0,), name='dec_cond')
        x = concatenate([x_inputs, cond_inputs], name='dec_input')

        nLayers=len(self.d_dims)
        for idx, layer_dim in reversed(list(enumerate(self.d_dims))):
            #x = Dense(units=layer_dim, activation='relu', name='dec_dense_{}'.format(idx))(x)
            if(idx==0):
                x = concatenate([Dense(units=layer_dim, activation='relu')(x), x],name="dec_dense_resnet{}".format(idx)) 
                #x = Dense(units=layer_dim, activation='relu',name="dec_dense_resnet{}".format(idx))(x) 
            else:
                if (idx==0 and self.embeddingBeforeLatent):#we make the embedding more influential
                    #x = concatenate([Dense(units=layer_dim, activation='relu')(x), cond_inputs], name="enc_dense_resnet{}".format(idx)) #plus rapide dans l'apprentissage mais sans doute moins scalable..!
                    print('cool')
                    x = concatenate([Dense(units=layer_dim, activation='relu')(x), x],name="dec_dense_resnet{}".format(idx)) 

                else:
                    #x = Dense(units=layer_dim, activation='relu',name="dec_dense_resnet{}".format(idx))(x) 
                    if(self.has_skip):
                        x = concatenate([Dense(units=layer_dim, activation='relu')(x), x],name="dec_dense_resnet{}".format(idx)) 
                    else:
                        x = Dense(units=layer_dim, activation='relu',name="dec_dense_resnet{}".format(idx))(x) 
            #x = Dropout(self.dropout)(x)
        #xprevious=x
        output = Dense(units=self.input_dim, activation='linear', name='dec_x_hat')(x)
        #outputBis = Lambda(lambda x: x)(x)

        return Model(inputs=[x_inputs, cond_inputs], outputs=output, name='decoder')

    def build_leap_block(self):

        tau_inputs = Input(shape=(self.tau_dims,), name='leap_enc_cond')

        x= Input(shape=(self.z_dim,), name='leap_enc_inputs')
        
        leap_inputs = [x,tau_inputs]

        Layers = len(self.e_leap_dims)
        for idx, layer_dim in enumerate(self.e_leap_dims):
            #x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
            x = Dense(units=layer_dim, activation='relu', name="leap_enc_dense_{}".format(idx))(x)
            #x = Dropout(self.dropout)(x)

        #z_mu = Dense(units=self.z_dim, activation='linear', name="latent_dense_mu")(x)
        #z_log_sigma = Dense(units=self.z_dim, activation='linear', name='latent_dense_log_sigma')(x)
        #x = Dense(units=self.z_dim, activation='relu', name="enc_dense_zdim")(x)
        tau_leap = Dense(units=self.tau_dims, use_bias=False, activation='relu', name="leap_dense_latent")(x)

        leap_product = Multiply()([tau_leap, tau_inputs])

        for idx, layer_dim in reversed(list(enumerate(self.e_leap_dims))):
            #x = Dense(units=layer_dim, activation='relu', name="enc_dense_{}".format(idx))(x)
            leap_product = Dense(units=layer_dim, activation='relu', name="leap_dec_dense_{}".format(idx))(leap_product)

        leap_output = Dense(units=self.z_dim, activation='linear', name="latent_dense_leap")(leap_product)

        return Model(inputs=leap_inputs, outputs=leap_output, name='leap_block')

    def build_loss(self, z_mu, z_log_sigma,beta=0):
        """

        :return:
        """

        def kl_loss(y_true, y_pred):
            if self.anisotropic_prior is None:
                if self.prior == 'Gaussian':
                    return 0.5 * K.sum(K.exp(z_log_sigma) + K.square(z_mu) - 1. - z_log_sigma, axis=-1)
                elif self.prior == 'Laplace':
                    return K.sum(K.abs(z_mu) + K.exp(z_log_sigma)*K.exp(-K.abs(z_mu)/K.exp(z_log_sigma)) - 1. - z_log_sigma, axis=-1)
            else:
                if self.prior == 'Gaussian':
                    return 0.5 * K.sum(self.anisotropic_prior + (K.exp(z_log_sigma) + K.square(z_mu)) / K.exp(self.anisotropic_prior) - 1. - z_log_sigma, axis=-1)
                
                elif self.prior == 'Laplace':
                    return K.sum((K.abs(z_mu) + K.exp(z_log_sigma)*K.exp(-K.abs(z_mu)/K.exp(z_log_sigma))) / K.exp(self.anisotropic_prior) - 1. - z_log_sigma + self.anisotropic_prior, axis=-1)

        def recon_loss(y_true, y_pred):
            if(self.is_L2_Loss):
                print("L2 loss")
                print(self.is_L2_Loss)
                return K.sum(K.square(y_pred - y_true), axis=-1)
            else:
                print("L1 loss")
                print(self.is_L2_Loss)
                return K.sum(K.abs(y_pred - y_true), axis=-1)

        def vae_loss(y_true, y_pred, beta=0, gamma=0):
            """ Calculate loss = reconstruction loss + KL loss for each data in minibatch """

            # E[log P(X|z,y)]
            recon = recon_loss(y_true=y_true, y_pred=y_pred)

            # D_KL(Q(z|X,y) || P(z|X)); calculate in closed form as both dist. are Gaussian
            kl = kl_loss(y_true=y_true, y_pred=y_pred)

            return recon + beta*kl

        return vae_loss, recon_loss, kl_loss
    
    def build_loss_info(self, z_mu, z_log_sigma, z,beta=0.5, gamma=1):
        """

        :return:
        """

        def kl_loss(y_true, y_pred):
            if self.pdf_model == 'Gaussian':
                return 0.5 * K.sum(K.exp(z_log_sigma) + K.square(z_mu) - 1. - z_log_sigma, axis=-1)
            elif self.pdf_model == 'Laplace':
                return K.sum(K.abs(z_mu) + K.exp(z_log_sigma)*K.exp(-K.abs(z_mu)/K.exp(z_log_sigma)) - 1. - z_log_sigma, axis=-1)


        def recon_loss(y_true, y_pred):
            if(self.is_L2_Loss):
                print("L2 loss")
                print(self.is_L2_Loss)
                return K.sum(K.square(y_pred - y_true), axis=-1)
            else:
                print("L1 loss")
                print(self.is_L2_Loss)
                return K.sum(K.abs(y_pred - y_true), axis=-1)

        def kde(s1,s2,h=None):
            dim = K.shape(s1)[1]
            s1_size = K.shape(s1)[0]
            s2_size = K.shape(s2)[0]
            if h is None:
                h = K.cast(dim, dtype='float32') / 2
            tiled_s1 = K.tile(K.reshape(s1, K.stack([s1_size, 1, dim])), K.stack([1, s2_size, 1]))
            tiled_s2 = K.tile(K.reshape(s2, K.stack([1, s2_size, dim])), K.stack([s1_size, 1, 1]))
            return K.exp(-0.5 * K.sum(K.square(tiled_s1 - tiled_s2), axis=-1)  / h)

        def info_loss(y_true, y_pred):
            q_kernel = kde(z_mu, z_mu)
            p_kernel = kde(z, z)
            pq_kernel = kde(z_mu, z)
            return K.mean(q_kernel) + K.mean(p_kernel) - 2 * K.mean(pq_kernel)

        def vae_loss(y_true, y_pred, beta=0, gamma=0):
            """ Calculate loss = reconstruction loss + KL loss for each data in minibatch """

            # E[log P(X|z,y)]
            recon = recon_loss(y_true=y_true, y_pred=y_pred)

            # D_KL(Q(z|X,y) || P(z|X)); calculate in closed form as both dist. are Gaussian
            kl = kl_loss(y_true=y_true, y_pred=y_pred)

            #D(q(z)|| p(z)); calculated with the MMD estimator using a Gaussian kernel
            info = info_loss(y_true=y_true, y_pred=y_pred)

            return recon + beta*kl + gamma*info

        return vae_loss, recon_loss, kl_loss, info_loss


    def train(self, dataset_train, training_epochs=10, batch_size=20, callbacks = [], validation_data = None, verbose = True,validation_split=None):
        """

        :param dataset_train:
        :param training_epochs:
        :param batch_size:
        :param callbacks:
        :param validation_data:
        :param verbose:
        :return:
        """

        assert len(dataset_train) >= 2  # Check that both x and cond are present
        #outputs=np.array([dataset_train['y'],dataset_train['y1']])
        output1=dataset_train['y']
        output2=dataset_train['y']

        if self.InfoVAE:
            output3=dataset_train['y']

            cvae_hist = self.cvae.fit(dataset_train['x'], [output1,output2,output3], batch_size=batch_size, epochs=training_epochs,
                                 validation_data=validation_data,validation_split=validation_split,
                                 callbacks=callbacks, verbose=verbose)
        else:
            cvae_hist = self.cvae.fit(dataset_train['x'], [output1,output2], batch_size=batch_size, epochs=training_epochs,
                                 validation_data=validation_data,validation_split=validation_split,
                                 callbacks=callbacks, verbose=verbose)

        return cvae_hist

class LEAP_CVAE_emb(LEAP_CVAE):
    """
    Improvement of CVAE that encode the temperature as a condition
    """
    def __init__(self, to_emb_dim=96, cond_pre_dim=12, emb_dims=[2], emb_to_z_dim=[3],is_emb_Enc_equal_emb_Dec=True, **kwargs):

        self.to_emb_dim = to_emb_dim
        self.cond_pre_dim = cond_pre_dim
        self.emb_dims = emb_dims
        self.emb_to_z_dim=emb_to_z_dim
        self.embedding_enc = None
        self.embedding_dec = None
        self.is_emb_Enc_equal_emb_Dec=is_emb_Enc_equal_emb_Dec
        
        cond_dims=self.cond_pre_dim 
        if(len(self.emb_to_z_dim)>=1):
            cond_dims=self.cond_pre_dim + self.emb_to_z_dim[-1]
        print(cond_dims)
        super().__init__(cond_dims=cond_dims,**kwargs)

    def build_model(self):
        """

        :param verbose:
        :return:
        """

        self.encoder = self.build_encoder()
        self.decoder = self.build_decoder()
        
        if(len(self.emb_to_z_dim)>=1):
            self.embedding_enc = self.build_embedding(name_emb='embedding_enc')
            if(self.is_emb_Enc_equal_emb_Dec):
                self.embedding_dec = self.embedding_enc
            else:
                self.embedding_dec = self.build_embedding(name_emb='embedding_dec')

        x_true = Input(shape=(self.input_dim,), name='x_true')
        tau_true = Input(shape=(self.tau_dims,), name='tau_true')
        inputs=[x_true, tau_true]
        xembs=[]
        cond_pre=[]
        if(self.cond_pre_dim>=1):
            cond_pre = Input(shape=(self.cond_pre_dim,), name='cond_pre')
            inputs.append(cond_pre)
        for j, cond in enumerate(self.to_emb_dim):#on enumere sur les conditions
            to_emb_dim=self.to_emb_dim[j]
            x_input = Input(shape=(to_emb_dim,), name='emb_input_{}'.format(j))
            xembs.append(x_input)
            inputs.append(x_input)
        
        cond_true_enc=[]
        cond_true_dec=[]#meme embedding que cond_true_enc en l etat
        if(len(self.emb_to_z_dim)>=1):
            cond_emb = self.embedding_enc(xembs)
            cond_true_enc =cond_emb 
            cond_emb2 = self.embedding_dec(xembs)
            if(self.is_emb_Enc_equal_emb_Dec):
                cond_true_dec =cond_emb 
            else:
                print('enc different de dec')
                cond_true_dec=cond_emb2
        if((self.cond_pre_dim>=1) and (len(self.emb_to_z_dim)>=1)):
            cond_true_enc = concatenate([cond_pre, cond_emb], name='conc_cond_enc')
            if(self.is_emb_Enc_equal_emb_Dec):
                cond_true_dec = concatenate([cond_pre, cond_emb], name='conc_cond_dec')
            else:
                print('enc different de dec 2')
                cond_true_dec = concatenate([cond_pre, cond_emb2], name='conc_cond_dec')
        elif(self.cond_pre_dim>=1):
            cond_true=cond_pre

        # Encoding
        z_mu, z_log_sigma = self.encoder([x_true, tau_true, cond_true_enc])
        #self.latent=Model(inputs=[x_inputs], outputs=[z_mu], name='z_mu')
        #self.latent=Lambda(lambda x: x,name='latent')(z_mu)
        x_inputs = Input(shape=(self.input_dim,), name='x_true_zmu_Layer') 
        x = Lambda(lambda x: x,name='z_mu')(x_inputs)
        self.latent=Model(inputs=[x_inputs], outputs=[x], name='z_mu_output')
        ZMU=self.latent(z_mu)

        # Sampling
        def sample_z(args):
            mu, log_sigma = args
            if self.pdf_model=='Gaussian':
                eps = K.random_normal(shape=(K.shape(mu)[0], self.z_dim), mean=0., stddev=1.)
                return mu + K.exp(log_sigma / 2) * eps
            elif self.pdf_model=='Laplace':
                U = K.random_uniform(shape=(K.shape(mu)[0], self.z_dim), minval =0.0, maxval=1.)
                V = K.random_uniform(shape=(K.shape(mu)[0], self.z_dim), minval =0.0, maxval=1.)
                Rad_sample = 2.*K.cast(K.greater_equal(V,0.5), dtype='float32') - 1. 
                Expon_sample = -K.exp(log_sigma)*K.log(1-U)
                return mu + Rad_sample*Expon_sample
        

        z = Lambda(sample_z, name='sample_z')([z_mu, z_log_sigma])

        # Decoding
        x_hat = self.decoder([z, cond_true_dec])
        
        #identity layer to have two output layers and compute separately 2 losses (the kl and the reconstruction)
        x_inputs = Input(shape=(self.input_dim,), name='x_true_identity_Layer')         
        x = Lambda(lambda x: x)(x_inputs)
        identitModel=Model(inputs=[x_inputs], outputs=[x], name='decoder_for_kl')
        
        xhatBis=identitModel(x_hat)

        if self.InfoVAE:
            identitModel2=Model(inputs=[x_inputs], outputs=[x], name='decoder_info')
            xhatTer=identitModel2(x_hat) 

            vae_loss, recon_loss, kl_loss, info_loss = self.build_loss_info(z_mu, z_log_sigma,z,beta=self.beta, gamma=self.gamma)

            # Defining and compiling cvae model
            self.losses = {"decoder": recon_loss,"decoder_for_kl": kl_loss, "decoder_info": info_loss}
            #lossWeights = {"decoder": 1.0, "decoder_for_kl": 0.01}
            self.weight_losses = {"decoder": self.alpha, "decoder_for_kl": self.beta, "decoder_info": self.gamma}

            model_outputs = [x_hat,xhatBis, xhatTer]
            
        else:
            # Defining loss
            vae_loss, recon_loss, kl_loss = self.build_loss(z_mu, z_log_sigma,beta=self.beta)

            # Defining and compiling cvae model
            self.losses = {"decoder": recon_loss,"decoder_for_kl": kl_loss}
            #lossWeights = {"decoder": 1.0, "decoder_for_kl": 0.01}
            self.weight_losses = {"decoder": self.alpha, "decoder_for_kl": self.beta}

            model_outputs = [x_hat,xhatBis]

        self.cvae = Model(inputs=inputs, outputs=model_outputs)

        Opt_Adam = optimizers.Adam(lr=self.lr)
        self.cvae.compile(optimizer=Opt_Adam,loss=self.losses,loss_weights=self.weight_losses)

    
        # Store trainers
        self.store_to_save('cvae')

        self.store_to_save('encoder')
        self.store_to_save('decoder')

        if self.verbose:
            print("complete model: ")
            self.cvae.summary()
            print("embedding_enc: ")
            if(len(self.emb_to_z_dim)>=1):
                self.embedding_enc.summary()
            print("encoder: ")
            self.encoder.summary()
            print("decoder: ")
            self.decoder.summary()
    

    def build_embedding(self,name_emb='embedding'):
        """
        Embedding of the temperature
        :return:
        """

        #verifier que les dimensions des inputs sont cohérentes
        if(len(self.emb_dims)!=len(self.to_emb_dim) ):
            print("dimensions du nombre de conditions dans les embeddings incoherent")
        xinputs=[]
        embeddings=[]
        x_input=[]
        
        for j, cond in enumerate(self.emb_dims):#on enumere sur les conditions
            to_emb_dim=self.to_emb_dim[j]
            x_input = Input(shape=(to_emb_dim,), name="emb_input_{}".format(j))
            xinputs.append(x_input)
            x = x_input
            
            nLayersCond=len(cond)
            for idx, layer_dim in enumerate(cond):
                if(idx==nLayersCond-1):
                    x = Dense(units=layer_dim, activation=None, name="emb_dense_{}_{}".format(j,idx))(x)
                    
                    ###############
                    if(self.has_BN==2):
                        x = BatchNormalization()(x)
                    ##############
                    x = Activation('relu')(x)
                else:
                    x = Dense(units=layer_dim, activation='relu', name="emb_dense_{}_{}".format(j,idx))(x)  
            if not cond:
                x = Dense(units=to_emb_dim, activation='linear', name="emb_linear_{}".format(j))(x)
            embeddings.append(x)
        
        if(len(xinputs)>=2):
            embedding_last= concatenate(embeddings, name='emb_concat')
        else:
            embedding_last=embeddings[0]  
        #embedding = Dense(units=self.emb_dims[-1], activation='linear', name="emb_dense_last")(x)

        emb_size=embedding_last.get_shape()[1]
        
        firstEmbDim=self.emb_to_z_dim[0]
        #if(emb_size>firstEmbDim):
        for j, layer_dim in enumerate(self.emb_to_z_dim):#on enumere sur les conditions
            embedding_last = Dense(units=layer_dim, activation=None,name="emb_dense_last_reduction_{}".format(j))(embedding_last)
               
            ######################
            if(self.has_BN>=1):
                embedding_last = BatchNormalization()(embedding_last)
            ############
               
            #embedding_last = Activation('relu')(embedding_last)
            embedding_last = Activation('relu')(embedding_last)
        
        model=Model(inputs=xinputs, outputs=embedding_last, name=name_emb)
        print(model.summary())
        return model
    
    def freezeLayers(self,mondule_names=['encoder']):
        
        if('encoder' in mondule_names):
            for layer in self.encoder.layers:
                layer.trainable = False
        if('decoder' in mondule_names):
            for layer in self.decoder.layers:
                layer.trainable = False
        if('embedding_enc' in mondule_names):
            print('embedding_enc')
            for layer in self.embedding_enc.layers:
                layer.trainable = False
                print(layer.name)
        if('embedding_dec' in mondule_names):
            print('embedding_dec')
            for layer in self.embedding_dec.layers:
                layer.trainable = False
                print(layer.name)
        self.cvae.compile(optimizer='Adam',loss=self.losses,loss_weights=self.weight_losses)
                
    def unfreezeLayers(self,mondule_names=['encoder']):
        
        if('encoder' in mondule_names):
            input_names=self.encoder.input_names
            for layer in self.encoder.layers:
                if(layer.name not in input_names):
                    layer.trainable = True
                    
        if('decoder' in mondule_names):
            input_names=self.decoder.input_names
            for layer in self.decoder.layers:
                if(layer.name not in input_names):
                    layer.trainable = True
                
        if('embedding_enc' in mondule_names):
            input_names=self.embedding_enc.input_names
            for layer in self.embedding_enc.layers:
                if(layer.name not in input_names):
                    layer.trainable = True
        
        if('embedding_dec' in mondule_names):
            input_names=self.embedding_dec.input_names
            for layer in self.embedding_dec.layers:
                if(layer.name not in input_names):
                    layer.trainable = True
        
        self.cvae.compile(optimizer='Adam',loss=self.losses,loss_weights=self.weight_losses)
        
    def updateLossWeight(self,newBeta=0.1):
        
        weightVar=self.cvae.loss_weights['decoder_for_kl']
        K.set_value(weightVar,newBeta)
    
    def printWeights(self,mondule_names=['encoder']):
        if('encoder' in mondule_names):
            input_names=self.encoder.input_names
            for layer in self.encoder.layers:
                if(layer.name not in input_names):
                    layer.trainable = True
                    
        if('decoder' in mondule_names):
            input_names=self.decoder.input_names
            for layer in self.decoder.layers:
                if(layer.name not in input_names):
                    layer.trainable = True
                
        if('embedding_enc' in mondule_names):
            input_names=self.embedding_enc.input_names
            for layer in self.embedding_enc.layers:
                if(layer.name not in input_names):
                    layer.trainable = True
        
        if('embedding_dec' in mondule_names):
            input_names=self.embedding_dec.input_names
            for layer in self.embedding_dec.layers:
                if(layer.name not in input_names):
                    layer.trainable = True


    
