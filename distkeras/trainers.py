"""Model optimizers. Depending on the implementation, these classes will optimize the
Keras model in a distributed manner (with exception of the SingleTrainer)."""

## BEGIN Imports. ##############################################################

import threading

import time

from distkeras.parameter_servers import DeltaParameterServer
from distkeras.parameter_servers import ADAGParameterServer
from distkeras.utils import deserialize_keras_model
from distkeras.utils import serialize_keras_model
from distkeras.networking import determine_host_address
from distkeras.workers import SequentialWorker
from distkeras.workers import AEASGDWorker
from distkeras.workers import DOWNPOURWorker
from distkeras.workers import EAMSGDWorker
from distkeras.workers import ADAGWorker

## END Imports. ################################################################

class Trainer(object):
    """Abstract trainer class. This class provides all base functionality which
    all optimizers need to implement.

    # Arguments
        keras_model: Keras model.
        loss: string. String representing the loss.
              See: https://keras.io/objectives/
        worker_optimizer: string. String representing worker optimizer.
                          See https://keras.io/optimizers/
    """

    def __init__(self, keras_model, loss, worker_optimizer):
        self.master_model = serialize_keras_model(keras_model)
        self.loss = loss
        self.worker_optimizer = worker_optimizer
        self.history = []
        self.training_time_start = 0
        self.training_time_end = 0
        self.training_time = 0

    def record_training_start(self):
        """Records the start of the training.

        This private function is called when the training process starts.
        """
        self.training_time = 0
        self.training_time_start = time.time()

    def record_training_end(self):
        """Records the end of the traing.

        This private function is called when the training process is terminated.
        """
        self.training_time_end = time.time()
        self.training_time = self.training_time_end - self.training_time_start

    def get_training_time(self):
        """Returns the told training time."""
        return self.training_time

    def get_history(self):
        """Returns all history object aggregated during training."""
        return self.history

    def has_history(self):
        """Check if there is any history available."""
        return len(self.history) > 0

    def add_history(self, history):
        """Adds an history object to the history list."""
        self.history.append(history)

    def train(self, dataframe, shuffle=False):
        """Trains the specified model using the specified dataframe.

        # Arguments
            dataframe: dataframe. Spark Dataframe
            shuffle: boolean. Tells to shuffle the dataframe before training.
                     Warning: this will tell Spark to shuffle all partitions over
                     the network. It is recommended to shuffle the dataset before
                     training and store it.
        """
        raise NotImplementedError


class SingleTrainer(Trainer):
    """An optimizer which will train a network on a single machine.

    # Arguments
        keras_model: model. Keras model to train.
        worker_optimizer: string. String representing worker optimizer.
                          See https://keras.io/optimizers/
        loss: string. String representing the loss.
              See: https://keras.io/objectives/
        features_col: string. Name of the features column.
        label_col: string. Name of the label column.
        num_epoch: int. Number of epochs.
        batch_size: int. Mini-batch size.
    """

    def __init__(self, keras_model, worker_optimizer, loss, features_col="features",
                 label_col="label", num_epoch=1, batch_size=32):
        super(SingleTrainer, self).__init__(keras_model, loss, worker_optimizer)
        self.features_column = features_col
        self.label_column = label_col
        self.num_epoch = num_epoch
        self.batch_size = batch_size

    def allocate_worker(self):
        """Allocates a worker for the Single Trainer instance.

        Only for internal use.
        """
        worker = SequentialWorker(model=self.master_model, features_col=self.features_column,
                                  label_col=self.label_column, batch_size=self.batch_size,
                                  optimizer=self.worker_optimizer, loss=self.loss)

        return worker

    def train(self, dataframe, shuffle=False):
        """See distkeras.trainers.Trainer.train

        # Arguments
            dataframe: dataframe. Spark Dataframe
            shuffle: boolean. Tells to shuffle the dataframe before training.
                     Warning: this will tell Spark to shuffle all partitions over
                     the network. It is recommended to shuffle the dataset before
                     training and store it.
        """
        # Check if the data needs to be shuffled.
        if shuffle:
            dataframe = shuffle(dataframe)
        # Collect all the data on a single worker node.
        dataframe = dataframe.coalesce(1)
        # Start recording training time.
        self.record_training_start()
        # Iterate through the number of records.
        for i in range(0, self.num_epoch):
            # Allocate a worker.
            worker = self.allocate_worker()
            # Fetch the trained model.
            self.master_model = dataframe.rdd.mapPartitionsWithIndex(worker.train).collect()[0]
        # Stop recording of training time.
        self.record_training_end()

        return deserialize_keras_model(self.master_model)


class AveragingTrainer(Trainer):
    """A trainer which implements a data parallel technique using model averaging.

    In this implementation, the model replicas are averages after every epoch.
    # Arguments
        keras_model: model. Keras model to train.
        worker_optimizer: string. String representing worker optimizer.
                          See https://keras.io/optimizers/
        loss: string. String representing the loss.
              See: https://keras.io/objectives/
        features_col: string. Name of the features column.
        label_col: string. Name of the label column.
        num_epoch: int. Number of epochs.
        batch_size: int. Mini-batch size.
        num_workers: int. Number of model replicas to train in parallel.
    """

    def __init__(self, keras_model, worker_optimizer, loss, features_col="features",
                 label_col="label", num_epoch=1, batch_size=32, num_workers=2):
        super(AveragingTrainer, self).__init__(keras_model, loss, worker_optimizer)
        self.features_column = features_col
        self.label_column = label_col
        self.num_epoch = num_epoch
        self.batch_size = batch_size
        self.num_workers = num_workers

    def allocate_worker(self):
        """Allocates the AveragingWorker for internal use."""
        raise NotImplementedError

    def train(self, dataframe, shuffle=False):
        """Applies model averaging to the model replicas distributed over the specified
        number of Spark executors.

        # Arguments
            dataframe: dataframe: A Spark Dataframe containing the dataset.
            shuffle: boolean. Tells to shuffle the dataframe before training.
                     Warning: this will tell Spark to shuffle all partitions over
                     the network. It is recommended to shuffle the dataset before
                     training and store it.
        """
        raise NotImplementedError


class EnsembleTrainer(Trainer):
    """Utility trainer which will train ensemble methods in parallel.

    # Arguments
        keras_model: model. Keras model to train.
        worker_optimizer: string. String representing worker optimizer.
                          See https://keras.io/optimizers/
        loss: string. String representing the loss.
              See: https://keras.io/objectives/
        features_col: string. Name of the features column.
        label_col: string. Name of the label column.
        batch_size: int. Mini-batch size.
        num_ensembles: int. Number of ensembles to train.
    # Note
        This will note employ a data-parallell approach for the ensembles.
    """

    def __init__(self, keras_model, worker_optimizer, loss, features_col="features",
                 label_col="label", batch_size=32, num_ensembles=2):
        super(EnsembleTrainer, self).__init__(keras_model, loss, worker_optimizer)
        self.features_column = features_col
        self.label_column = label_col
        self.batch_size = batch_size
        self.num_ensembles = num_ensembles

    def allocate_worker(self):
        """Allocates the EnsembleWorker for internal use."""
        worker = SequentialWorker(model=self.master_model, features_col=self.features_column,
                                  label_col=self.label_column, batch_size=self.batch_size,
                                  optimizer=self.worker_optimizer, loss=self.loss)

        return worker

    def train(self, dataframe, shuffle=False):
        """Trains the specified number of ensemble models using the specified dataframe.

        # Arguments
            dataframe: dataframe: A Spark Dataframe containing the dataset.
            shuffle: boolean. Tells to shuffle the dataframe before training.
                     Warning: this will tell Spark to shuffle all partitions over
                     the network. It is recommended to shuffle the dataset before
                     training and store it.
        """
        # Allocate a worker.
        worker = self.allocate_worker()
        # Repartition in order to fit the number of workers.
        num_partitions = dataframe.rdd.getNumPartitions()
        # Check if the dataframe needs to be shuffled before training.
        if shuffle:
            dataframe = shuffle(dataframe)
        # Check if we need to repartition the dataframe.
        if num_partitions > self.num_workers:
            dataframe = dataframe.coalesce(self.num_workers)
        else:
            dataframe = dataframe.repartition(self.num_workers)
        # Start the training procedure.
        self.record_training_start()
        # Train the models in parallel.
        models = dataframe.rdd.mapPartitionsWithIndex(worker.train).collect()
        # End the training procedure.
        self.record_training_end()

        return models


class DistributedTrainer(Trainer):
    """Abstract class which describes the properties of a distributed optimizer.

    # Arguments
        keras_model: model. Keras model to train.
        worker_optimizer: string. String representing worker optimizer.
                          See https://keras.io/optimizers/
        loss: string. String representing the loss.
              See: https://keras.io/objectives/
        features_col: string. Name of the features column.
        label_col: string. Name of the label column.
        num_epoch: int. Number of epochs.
        batch_size: int. Mini-batch size.
        num_workers: int. Number of distributed workers.
    """

    def __init__(self, keras_model, worker_optimizer, loss, num_workers=2, batch_size=32,
                 features_col="features", label_col="label", num_epoch=1):
        super(DistributedTrainer, self).__init__(keras_model, loss, worker_optimizer)
        self.num_workers = num_workers
        self.batch_size = batch_size
        self.features_column = features_col
        self.label_column = label_col
        self.num_epoch = num_epoch
        self.parameter_server = None
        self.parameter_server_thread = None
        self.master_host = determine_host_address()
        self.master_port = 5000

    def allocate_worker(self):
        """Allocates the worker implementation.

        Implement this method in subclasses.
        """
        raise NotImplementedError

    def allocate_parameter_server(self):
        """Allocates the parameter server.

        If an other type of parameter server is required, you can overwrite
        this implementation.
        """
        parameter_server = DeltaParameterServer(self.master_model, self.master_port)

        return parameter_server

    def num_updates(self):
        """Returns the number of model updates the parameter server performed."""
        return self.parameter_server.num_updates()

    def service(self):
        """Executes the parameter server service."""
        self.parameter_server.start()
        self.parameter_server.initialize()
        self.parameter_server.run()

    def stop_service(self):
        """Stops the parameter server service."""
        self.parameter_server.stop()
        self.parameter_server_thread.join()
        self.parameter_server_thread = None

    def start_service(self):
        """Starts the parameter server service."""
        # Check if a parameter server thread is already allocated.
        if not self.parameter_server_thread is None:
            # Stop the parameter server service.
            self.stop_service()
        # Allocate a new parameter service thread.
        self.parameter_server_thread = threading.Thread(target=self.service)
        self.parameter_server_thread.start()

    def train(self, dataframe, shuffle=False):
        """Training procedure of a distributed optimization process.

        # Arguments
            dataframe: dataframe. Spark Dataframe
            shuffle: boolean. Tells to shuffle the dataframe before training.
                     Warning: this will tell Spark to shuffle all partitions over
                     the network. It is recommended to shuffle the dataset before
                     training and store it.
        """
        # Allocate the parameter server.
        self.parameter_server = self.allocate_parameter_server()
        # Start the communication service.
        self.start_service()
        # Allocate a worker.
        worker = self.allocate_worker()
        # Repartition in order to fit the number of workers.
        num_partitions = dataframe.rdd.getNumPartitions()
        # Check if the dataframe needs to be shuffled before training.
        if shuffle:
            dataframe = shuffle(dataframe)
        # Check if we need to repartition the dataframe.
        if num_partitions > self.num_workers:
            dataframe = dataframe.coalesce(self.num_workers)
        else:
            dataframe = dataframe.repartition(self.num_workers)
        # Start the training procedure.
        self.record_training_start()
        # Iterate through the epochs.
        for i in range(0, self.num_epoch):
            dataframe.rdd.mapPartitionsWithIndex(worker.train).collect()
        # End the training procedure.
        self.record_training_end()
        # Stop the communication service.
        self.stop_service()

        return self.parameter_server.get_model()


class AsynchronousDistributedTrainer(DistributedTrainer):
    """Abstract class for an asynchronous distributed trainer.

    This trainer also allows us to set a parallelism factor. This parallelism factor allows
    us to further parallelize the Spark job. For example, imagine having n machines optimizing
    a model in an asynchronous distributed setting. If for some, but likely reason, some machines
    are performing worse compared to others. It will cause the complete learning procedure to be
    stuck on this one particular machine since every machine will be assigned a single partition.
    In order to resolve this, we added a parallelization factor. This factor indicates the ratio
    of the number of jobs per machine (executor). For small datasets, we recommend that this factor
    is set to 1. However, this effect really is prominent when the dataset is large. In this case
    we recommend that the ratio is 2 or 3.

    # Arguments
        keras_model: model. Keras model to train.
        worker_optimizer: string. String representing worker optimizer.
                          See https://keras.io/optimizers/
        loss: string. String representing the loss.
              See: https://keras.io/objectives/
        features_col: string. Name of the features column.
        label_col: string. Name of the label column.
        num_epoch: int. Number of epochs.
        batch_size: int. Mini-batch size.
        num_workers: int. Number of distributed workers.

    # Note
        By default, the parallelization factor is set to 1.
    """

    def __init__(self, keras_model, worker_optimizer, loss, num_workers=2, batch_size=32,
                 features_col="features", label_col="label", num_epoch=1):
        super(AsynchronousDistributedTrainer, self).__init__(keras_model, worker_optimizer, loss,
                                                             num_workers, batch_size, features_col,
                                                             label_col, num_epoch)
        # Initialize asynchronous methods variables.
        self.parallelism_factor = 1

    def allocate_worker(self):
        """Allocates the worker implementation.

        Implement this method in subclasses.
        """
        raise NotImplementedError

    def set_parallelism_factor(self, factor):
        """Sets the parallelization factor.

        # Arguments
            factor: int. The new parallelization factor.
        """
        self.parallelism_factor = factor

    def get_parallelism_factor(self):
        """Returns the parallelization factor."""
        return self.parallelism_factor

    def train(self, dataframe, shuffle=False):
        """Training procedure of an asynchronous distributed optimization process.

        # Arguments
            dataframe: dataframe. Spark Dataframe
            shuffle: boolean. Tells to shuffle the dataframe before training.
                     Warning: this will tell Spark to shuffle all partitions over
                     the network. It is recommended to shuffle the dataset before
                     training and store it.
        """
        # Allocate the parameter server.
        self.parameter_server = self.allocate_parameter_server()
        # Start the communication service.
        self.start_service()
        # Allocate a worker.
        worker = self.allocate_worker()
        # Repartition in order to fit the number of workers.
        num_partitions = dataframe.rdd.getNumPartitions()
        # Check if the dataframe needs to be shuffled before training.
        if shuffle:
            dataframe = shuffle(dataframe)
        # Indicate the parallelism (number of worker times parallelism factor).
        parallelism = self.parallelism_factor * self.num_workers
        # Check if we need to repartition the dataframe.
        if num_partitions > parallelism:
            dataframe = dataframe.coalesce(parallelism)
        else:
            dataframe = dataframe.repartition(parallelism)
        # Start the training procedure.
        self.record_training_start()
        # Iterate through the epochs.
        for i in range(0, self.num_epoch):
            dataframe.rdd.mapPartitionsWithIndex(worker.train).collect()
        # End the training procedure.
        self.record_training_end()
        # Stop the communication service.
        self.stop_service()

        return self.parameter_server.get_model()


class DOWNPOUR(AsynchronousDistributedTrainer):
    """DOWNPOUR Optimizer.

    Asynchronous data-parallel optimizer introduced by Dean et al.
    http://static.googleusercontent.com/media/research.google.com/en/archive/large_deep_networks_nips2012.pdf

    # Arguments
        keras_model: model. Keras model to train.
        worker_optimizer: string. String representing worker optimizer.
                          See https://keras.io/optimizers/
        loss: string. String representing the loss.
              See: https://keras.io/objectives/
        features_col: string. Name of the features column.
        label_col: string. Name of the label column.
        num_epoch: int. Number of epochs.
        batch_size: int. Mini-batch size.
        num_workers: int. Number of distributed workers.
        communication_window: int. Staleness parameter.
                              This parameter describes the number of mini-batches that will be
                              computed before updating the center variable. For DOWNPOUR we
                              recommend small communication windows.
        learning_rate: float. Learning rate.
    """

    def __init__(self, keras_model, worker_optimizer, loss, num_workers=2, batch_size=32,
                 features_col="features", label_col="label", num_epoch=1, learning_rate=0.1,
                 communication_window=5):
        super(DOWNPOUR, self).__init__(keras_model, worker_optimizer, loss, num_workers,
                                       batch_size, features_col, label_col, num_epoch)
        self.learning_rate = learning_rate
        self.communication_window = communication_window

    def allocate_worker(self):
        """Allocates the DOWNPOUR worker."""
        # Allocate DOWNPOUR worker.
        worker = DOWNPOURWorker(self.master_model, self.worker_optimizer, self.loss,
                                self.features_column, self.label_column, self.batch_size,
                                self.master_host, self.master_port, self.learning_rate,
                                self.communication_window)

        return worker


class EAMSGD(AsynchronousDistributedTrainer):
    """Asynchronous Elastic Averaging w/ Momentum SGD optimizer.

    Introduced by Zhang et al.
    https://arxiv.org/pdf/1412.6651.pdf

    # Arguments
        keras_model: model. Keras model to train.
        worker_optimizer: string. String representing worker optimizer.
                          See https://keras.io/optimizers/
        loss: string. String representing the loss.
              See: https://keras.io/objectives/
        features_col: string. Name of the features column.
        label_col: string. Name of the label column.
        num_epoch: int. Number of epochs.
        batch_size: int. Mini-batch size.
        num_workers: int. Number of distributed workers.
        communication_window: int. Staleness parameter.
                              This parameter describes the number of mini-batches that will be
                              computed before updating the center variable. For EASGD based
                              algorithms we recommend large communication windows.
        learning_rate: float. Learning rate.
        rho: float. Elastic "exploration" variable.
                    Higher values mean that the model is allowed to "explore" its surroundings.
                    Smaller values are correlated with less exploration. We use the value
                    recommend by the authors.
        momentum: float. Momentum term.
    """

    def __init__(self, keras_model, worker_optimizer, loss, num_workers=2, batch_size=32,
                 features_col="features", label_col="label", num_epoch=1, communication_window=32,
                 rho=5.0, learning_rate=0.1, momentum=0.9):
        super(EAMSGD, self).__init__(keras_model, worker_optimizer, loss, num_workers,
                                     batch_size, features_col, label_col, num_epoch)
        self.communication_window = communication_window
        self.rho = rho
        self.learning_rate = learning_rate
        self.momentum = momentum

    def allocate_worker(self):
        """Allocates the asynchronous EAMSGD worker."""
        # Allocate a EAMSGD REST worker.
        worker = EAMSGDWorker(self.master_model, self.worker_optimizer, self.loss,
                              self.features_column, self.label_column, self.batch_size,
                              self.master_host, self.master_port, self.rho, self.learning_rate,
                              self.momentum, self.communication_window)

        return worker


class ADAG(AsynchronousDistributedTrainer):
    """Asynchronous Distributed Adaptive Gradient (Stochastic Gradient Descent).

    Introduced by Hermans et al.

    # Arguments:
        keras_model: model. Keras model to train.
        worker_optimizer: string. String representing worker optimizer.
                          See: https://keras.io/optimizers/
        loss: string. String representing the loss function.
              See: https://keras.io/objectives/
        features_col: string. Name of the label column.
        num_epoch: int. Number of epochs.
        batch_size: int. Mini-batch size.
        num_workers: int. Number of distributed workers.
        communication_window: int. Staleness parameter.
                              This parameter describes the number of mini-batches that will be
                              computed before updating the center variable. For DOWNPOUR based
                              algorithms we recommend large communication windows.
        learning_rate: float. Learning rate.
        beta_1: float. Default value 0.9
        beta_2: float. Default value 0.999
    """

    def __init__(self, keras_model, worker_optimizer, loss, num_workers=2, batch_size=32,
                 features_col="features", label_col="label", num_epoch=1, communication_window=5,
                 learning_rate=0.1, beta_1=0.9, beta_2=0.999):
        super(ADAG, self).__init__(keras_model, worker_optimizer, loss, num_workers,
                                   batch_size, features_col, label_col, num_epoch)
        # Set algorithm parameters.
        self.communication_window = communication_window
        self.learning_rate = learning_rate
        self.beta_1 = beta_1
        self.beta_2 = beta_2

    def allocate_worker(self):
        """Allocate an Adag worker."""
        worker = ADAGWorker(self.master_model, self.worker_optimizer, self.loss,
                            self.features_column, self.label_column, self.batch_size,
                            self.master_host, self.master_port, self.learning_rate,
                            self.communication_window)

    def allocate_parameter_server(self):
        """Allocate the Adag parameter server."""
        # Allocate an ADAGA parameter server.
        parameter_server = ADAGParameterServer(self.master_model, self.master_port,
                                               self.learning_rate, self.beta_1, self.beta_2)

        return parameter_server
