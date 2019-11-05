import tensorflow as tf
import csv
from nltk import word_tokenize
import string
import json, gzip
from pandas.io.json import json_normalize
import pandas as pd


#####


with open('processed.json') as file:

    for json_data in file:
        saved_data = json.loads(json_data)

        vocab2idx = saved_data["vocab"]
        embd = saved_data["embd"]
        train_batches_text = saved_data["train_batches_text"]
        test_batches_text = saved_data["test_batches_text"]
        val_batches_text = saved_data["val_batches_text"]
        train_batches_summary = saved_data["train_batches_summary"]
        test_batches_summary = saved_data["test_batches_summary"]
        val_batches_summary = saved_data["val_batches_summary"]
        train_batches_true_text_len = saved_data["train_batches_true_text_len"]
        val_batches_true_text_len = saved_data["val_batches_true_text_len"]
        test_batches_true_text_len = saved_data["test_batches_true_text_len"]
        train_batches_true_summary_len = saved_data["train_batches_true_summary_len"]
        val_batches_true_summary_len = saved_data["val_batches_true_summary_len"]
        test_batches_true_summary_len = saved_data["test_batches_true_summary_len"]

        break
        
idx2vocab = {v:k for k,v in vocab2idx.items()}


########

#HyperParameters
hidden_size = 400
learning_rate = 0.001
epochs = 5
max_summary_len = 51 # should be summary_max_len as used in data_preprocessing with +1 (+1 for <EOS>) 
D = 5 # D determines local attention window size
window_len = 2*D+1
l2=1e-6



###############

tf.reset_default_graph()

embd_dim = len(embd[0])

tf_text = tf.compat.v1.placeholder(tf.int32, [None, None])
tf_embd = tf.compat.v1.placeholder(tf.float32, [len(vocab2idx), embd_dim])
tf_true_summary_len = tf.compat.v1.placeholder(tf.int32, [None])
tf_summary = tf.compat.v1.placeholder(tf.int32, [None, None])
tf_train = tf.compat.v1.placeholder(tf.bool)


#Embed vectorize text
embd_text = tf.nn.embedding_lookup(tf_embd, tf_text)
embd_text = tf.layers.dropout(embd_text,rate=0.3,training=tf_train)



#LSTM function
def LSTM(x,hidden_state,cell,input_dim,hidden_size,scope):
    
    with tf.variable_scope(scope,reuse=tf.AUTO_REUSE):
        
        w = tf.get_variable("w", shape=[4,input_dim,hidden_size],
                                    dtype=tf.float32,
                                    trainable=True,
                                    initializer=tf.glorot_uniform_initializer())
        
        u = tf.get_variable("u", shape=[4,hidden_size,hidden_size],
                            dtype=tf.float32,
                            trainable=True,
                            initializer=tf.glorot_uniform_initializer())
        
        b = tf.get_variable("bias", shape=[4,1,hidden_size],
                    dtype=tf.float32,
                    trainable=True,
                    initializer=tf.zeros_initializer())
        
    input_gate = tf.nn.sigmoid( tf.matmul(x,w[0]) + tf.matmul(hidden_state,u[0]) + b[0])
    forget_gate = tf.nn.sigmoid( tf.matmul(x,w[1]) + tf.matmul(hidden_state,u[1]) + b[1])
    output_gate = tf.nn.sigmoid( tf.matmul(x,w[2]) + tf.matmul(hidden_state,u[2]) + b[2])
    cell_ = tf.nn.tanh( tf.matmul(x,w[3]) + tf.matmul(hidden_state,u[3]) + b[3])
    cell = forget_gate*cell + input_gate*cell_
    hidden_state = output_gate*tf.tanh(cell)
    
    return hidden_state, cell


#FORWARD ENCODING
S = tf.shape(embd_text)[1] #text sequence length
N = tf.shape(embd_text)[0] #batch_size

i=0
hidden=tf.zeros([N, hidden_size], dtype=tf.float32)
cell=tf.zeros([N, hidden_size], dtype=tf.float32)
hidden_forward=tf.TensorArray(size=S, dtype=tf.float32)

#shape of embd_text: [N,S,embd_dim]
embd_text_t = tf.transpose(embd_text,[1,0,2]) 
#current shape of embd_text: [S,N,embd_dim]

def cond(i, hidden, cell, hidden_forward):
    return i < S

def body(i, hidden, cell, hidden_forward):
    x = embd_text_t[i]
    
    hidden,cell = LSTM(x,hidden,cell,embd_dim,hidden_size,scope="forward_encoder")
    hidden_forward = hidden_forward.write(i, hidden)

    return i+1, hidden, cell, hidden_forward

_, _, _, hidden_forward = tf.while_loop(cond, body, [i, hidden, cell, hidden_forward])


#Backward Encoding
i=S-1
hidden=tf.zeros([N, hidden_size], dtype=tf.float32)
cell=tf.zeros([N, hidden_size], dtype=tf.float32)
hidden_backward=tf.TensorArray(size=S, dtype=tf.float32)

def cond(i, hidden, cell, hidden_backward):
    return i >= 0

def body(i, hidden, cell, hidden_backward):
    x = embd_text_t[i]
    hidden,cell = LSTM(x,hidden,cell,embd_dim,hidden_size,scope="backward_encoder")
    hidden_backward = hidden_backward.write(i, hidden)

    return i-1, hidden, cell, hidden_backward

_, _, _, hidden_backward = tf.while_loop(cond, body, [i, hidden, cell, hidden_backward])



#Merge Forward and Backward Encoder Hidden States
hidden_forward = hidden_forward.stack()
hidden_backward = hidden_backward.stack()

encoder_states = tf.concat([hidden_forward,hidden_backward],axis=-1)
encoder_states = tf.transpose(encoder_states,[1,0,2])

encoder_states = tf.layers.dropout(encoder_states,rate=0.3,training=tf_train)

final_encoded_state = tf.layers.dropout(tf.concat([hidden_forward[-1],hidden_backward[-1]],axis=-1),rate=0.3,training=tf_train)



#Implementation of Scoring function
def attention_score(encoder_states,decoder_hidden_state,scope="attention_score"):
    
    with tf.variable_scope(scope,reuse=tf.AUTO_REUSE):
        Wa = tf.get_variable("Wa", shape=[2*hidden_size,2*hidden_size],
                                    dtype=tf.float32,
                                    trainable=True,
                                    initializer=tf.glorot_uniform_initializer())
        
    encoder_states = tf.reshape(encoder_states,[N*S,2*hidden_size])
    
    encoder_states = tf.reshape(tf.matmul(encoder_states,Wa),[N,S,2*hidden_size])
    decoder_hidden_state = tf.reshape(decoder_hidden_state,[N,2*hidden_size,1])
    
    return tf.reshape(tf.matmul(encoder_states,decoder_hidden_state),[N,S])



#Local Attention Function
def align(encoder_states, decoder_hidden_state,scope="attention"):
    
    with tf.variable_scope(scope,reuse=tf.AUTO_REUSE):
        Wp = tf.get_variable("Wp", shape=[2*hidden_size,125],
                                    dtype=tf.float32,
                                    trainable=True,
                                    initializer=tf.glorot_uniform_initializer())
        
        Vp = tf.get_variable("Vp", shape=[125,1],
                            dtype=tf.float32,
                            trainable=True,
                            initializer=tf.glorot_uniform_initializer())
    
    positions = tf.cast(S-window_len,dtype=tf.float32) # Maximum valid attention window starting position
    
    # Predict attention window starting position 
    ps = positions*tf.nn.sigmoid(tf.matmul(tf.tanh(tf.matmul(decoder_hidden_state,Wp)),Vp))
    # ps = (soft-)predicted starting position of attention window
    pt = ps+D # pt = center of attention window where the whole window length is 2*D+1
    pt = tf.reshape(pt,[N])
    
    i = 0
    gaussian_position_based_scores = tf.TensorArray(size=S,dtype=tf.float32)
    sigma = tf.constant(D/2,dtype=tf.float32)
    
    def cond(i,gaussian_position_based_scores):
        
        return i < S
                      
    def body(i,gaussian_position_based_scores):
        
        score = tf.exp(-((tf.square(tf.cast(i,tf.float32)-pt))/(2*tf.square(sigma)))) 
        # (equation (10) in https://nlp.stanford.edu/pubs/emnlp15_attn.pdf)
        gaussian_position_based_scores = gaussian_position_based_scores.write(i,score)
            
        return i+1,gaussian_position_based_scores
                      
    i,gaussian_position_based_scores = tf.while_loop(cond,body,[i,gaussian_position_based_scores])
    
    gaussian_position_based_scores = gaussian_position_based_scores.stack()
    gaussian_position_based_scores = tf.transpose(gaussian_position_based_scores,[1,0])
    gaussian_position_based_scores = tf.reshape(gaussian_position_based_scores,[N,S])
    
    scores = attention_score(encoder_states,decoder_hidden_state)*gaussian_position_based_scores
    scores = tf.nn.softmax(scores,axis=-1)
    
    return tf.reshape(scores,[N,S,1])


with tf.variable_scope("decoder",reuse=tf.AUTO_REUSE):
    SOS = tf.get_variable("sos", shape=[1,embd_dim],
                                dtype=tf.float32,
                                trainable=True,
                                initializer=tf.glorot_uniform_initializer())
    
    # SOS represents starting marker 
    # It tells the decoder that it is about to decode the first word of the output
    # I have set SOS as a trainable parameter
    
    Wc = tf.get_variable("Wc", shape=[4*hidden_size,embd_dim],
                            dtype=tf.float32,
                            trainable=True,
                            initializer=tf.glorot_uniform_initializer())
    


SOS = tf.tile(SOS,[N,1]) #now SOS shape: [N,embd_dim]
inp = SOS
hidden=final_encoded_state
cell=tf.zeros([N, 2*hidden_size], dtype=tf.float32)
decoder_outputs=tf.TensorArray(size=max_summary_len, dtype=tf.float32)
outputs=tf.TensorArray(size=max_summary_len, dtype=tf.int32)

for i in range(max_summary_len):
    
    inp = tf.layers.dropout(inp,rate=0.3,training=tf_train)
    
    attention_scores = align(encoder_states,hidden)
    encoder_context_vector = tf.reduce_sum(encoder_states*attention_scores,axis=1)
    
    hidden,cell = LSTM(inp,hidden,cell,embd_dim,2*hidden_size,scope="decoder")
    
    hidden_ = tf.layers.dropout(hidden,rate=0.3,training=tf_train)
    
    concated = tf.concat([hidden_,encoder_context_vector],axis=-1)
    
    linear_out = tf.nn.tanh(tf.matmul(concated,Wc))
    decoder_output = tf.matmul(linear_out,tf.transpose(tf_embd,[1,0])) 
    # produce unnormalized probability distribution over vocabulary
    
    
    decoder_outputs = decoder_outputs.write(i,decoder_output)
    
    # Pick out most probable vocab indices based on the unnormalized probability distribution
    
    next_word_vec = tf.cast(tf.argmax(decoder_output,1),tf.int32)

    next_word_vec = tf.reshape(next_word_vec, [N])

    outputs = outputs.write(i,next_word_vec)

    next_word = tf.nn.embedding_lookup(tf_embd, next_word_vec)
    inp = tf.reshape(next_word, [N, embd_dim])
    
    
decoder_outputs = decoder_outputs.stack()
outputs = outputs.stack()

decoder_outputs = tf.transpose(decoder_outputs,[1,0,2])
outputs = tf.transpose(outputs,[1,0])


filtered_trainables = [var for var in tf.trainable_variables() if
                       not("Bias" in var.name or "bias" in var.name
                           or "noreg" in var.name)]

regularization = tf.reduce_sum([tf.nn.l2_loss(var) for var
                                in filtered_trainables])

with tf.variable_scope("loss"):

    epsilon = tf.constant(1e-9, tf.float32)

    cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
        labels=tf_summary, logits=decoder_outputs)

    pad_mask = tf.sequence_mask(tf_true_summary_len,
                                maxlen=max_summary_len,
                                dtype=tf.float32)

    masked_cross_entropy = cross_entropy*pad_mask

    cost = tf.reduce_mean(masked_cross_entropy) + \
        l2*regularization

    cross_entropy = tf.reduce_mean(masked_cross_entropy)
    

# Comparing predicted sequence with labels
comparison = tf.cast(tf.equal(outputs, tf_summary),
                     tf.float32)

# Masking to ignore the effect of pads while calculating accuracy
pad_mask = tf.sequence_mask(tf_true_summary_len,
                            maxlen=max_summary_len,
                            dtype=tf.bool)

masked_comparison = tf.boolean_mask(comparison, pad_mask)

# Accuracy
accuracy = tf.reduce_mean(masked_comparison)


all_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)

optimizer = tf.contrib.opt.NadamOptimizer(
    learning_rate=learning_rate)

gvs = optimizer.compute_gradients(cost, var_list=all_vars)

capped_gvs = [(tf.clip_by_norm(grad, 5), var) for grad, var in gvs] # Gradient Clipping

train_op = optimizer.apply_gradients(capped_gvs)


import pickle
import random

with tf.Session() as sess:  # Start Tensorflow Session
    display_step = 100
    patience = 5

    load = input("\nLoad checkpoint? y/n: ")
    print("")
    saver = tf.train.Saver()

    if load.lower() == 'y':

        print('Loading pre-trained weights for the model...')
        saver.restore(sess, './Model_Backup/Seq2seq_summarization.ckpt')
        sess.run(tf.global_variables())
        sess.run(tf.tables_initializer())

        with open('./Model_Backup/Seq2seq_summarization.pkl', 'rb') as fp:
            train_data = pickle.load(fp)

        covered_epochs = train_data['covered_epochs']
        best_loss = train_data['best_loss']
        impatience = 0
        
        print('\nRESTORATION COMPLETE\n')

    else:
        best_loss = 2**30
        impatience = 0
        covered_epochs = 0

        init = tf.global_variables_initializer()
        sess.run(init)
        sess.run(tf.tables_initializer())

    epoch=0
    while (epoch+covered_epochs)<epochs:
        
        print("\n\nSTARTING TRAINING\n\n")
        
        batches_indices = [i for i in range(0, len(train_batches_text))]
        random.shuffle(batches_indices)

        total_train_acc = 0
        total_train_loss = 0

        for i in range(0, len(train_batches_text)):
            
            j = int(batches_indices[i])

            cost,prediction,\
                acc, _ = sess.run([cross_entropy,
                                   outputs,
                                   accuracy,
                                   train_op],
                                  feed_dict={tf_text: train_batches_text[j],
                                             tf_embd: embd,
                                             tf_summary: train_batches_summary[j],
                                             tf_true_summary_len: train_batches_true_summary_len[j],
                                             tf_train: True})
            
            total_train_acc += acc
            total_train_loss += cost

            if i % display_step == 0:
                print("Iter "+str(i)+", Cost= " +
                      "{:.3f}".format(cost)+", Acc = " +
                      "{:.2f}%".format(acc*100))
            
            if i % 500 == 0:
                
                idx = random.randint(0,len(train_batches_text[j])-1)
                
                
                
                text = " ".join([idx2vocab.get(vec,"<UNK>") for vec in train_batches_text[j][idx]])
                predicted_summary = [idx2vocab.get(vec,"<UNK>") for vec in prediction[idx]]
                actual_summary = [idx2vocab.get(vec,"<UNK>") for vec in train_batches_summary[j][idx]]
                
                print("\nSample Text\n")
                print(text)
                print("\nSample Predicted Summary\n")
                for word in predicted_summary:
                    if word == '<EOS>':
                        break
                    else:
                        print(word,end=" ")
                print("\n\nSample Actual Summary\n")
                for word in actual_summary:
                    if word == '<EOS>':
                        break
                    else:
                        print(word,end=" ")
                print("\n\n")
                
        print("\n\nSTARTING VALIDATION\n\n")
                
        total_val_loss=0
        total_val_acc=0
                
        for i in range(0, len(val_batches_text)):
            
            if i%100==0:
                print("Validating data # {}".format(i))

            cost, prediction,\
                acc = sess.run([cross_entropy,
                                outputs,
                                accuracy],
                                  feed_dict={tf_text: val_batches_text[i],
                                             tf_embd: embd,
                                             tf_summary: val_batches_summary[i],
                                             tf_true_summary_len: val_batches_true_summary_len[i],
                                             tf_train: False})
            
            total_val_loss += cost
            total_val_acc += acc
            
        avg_val_loss = total_val_loss/len(val_batches_text)
        
        print("\n\nEpoch: {}\n\n".format(epoch+covered_epochs))
        print("Average Training Loss: {:.3f}".format(total_train_loss/len(train_batches_text)))
        print("Average Training Accuracy: {:.2f}".format(100*total_train_acc/len(train_batches_text)))
        print("Average Validation Loss: {:.3f}".format(avg_val_loss))
        print("Average Validation Accuracy: {:.2f}".format(100*total_val_acc/len(val_batches_text)))
              
        if (avg_val_loss < best_loss):
            best_loss = avg_val_loss
            save_data={'best_loss':best_loss,'covered_epochs':covered_epochs+epoch+1}
            impatience=0
            with open('./Model_Backup/Seq2seq_summarization.pkl', 'wb') as fp:
                pickle.dump(save_data, fp)
            saver.save(sess, save_path='./Model_Backup/Seq2seq_summarization.ckpt')
            print("\nModel saved\n")
              
        else:
            impatience+=1
              
        if impatience > patience:
              break
              
              
        epoch+=1


