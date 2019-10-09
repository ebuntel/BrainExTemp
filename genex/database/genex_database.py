import heapq
import json
import math
import os
import pickle
from pyspark import SparkContext
from pyspark.sql import SQLContext
import pandas as pd
import numpy as np
import shutil
from genex.classes.Sequence import Sequence
from genex.cluster import sim_between_seq, _cluster_groups, lb_kim_sequence, lb_keogh_sequence
from genex.preprocess import get_subsequences, genex_normalize, _group_time_series, _slice_time_series
from genex.utils import scale, _validate_gxdb_build_arguments, _df_to_list, _process_loi, _query_partition, \
    _validate_gxdb_query_arguments


def from_csv(file_name, feature_num: int, sc: SparkContext):
    """
    build a genex_database object from given csv,
    Note: if time series are of different length, shorter sequences will be post padded to the length
    of the longest sequence in the dataset

    :param file_name:
    :param feature_num:
    :param sc:
    :return:
    """

    df = pd.read_csv(file_name)
    data_list = _df_to_list(df, feature_num=feature_num)

    data_norm_list, global_max, global_min = genex_normalize(data_list, z_normalization=True)

    # return Genex_database
    return genex_database(data=data_list, data_normalized=data_norm_list, global_max=global_max, global_min=global_min,
                          spark_context=sc)


def from_db(sc: SparkContext, path: str):
    """

    :param sc:
    :param path:
    :return:
    """

    # TODO the input fold_name is not existed
    data = pickle.load(open(os.path.join(path, 'data.gxdb'), 'rb'))
    data_normalized = pickle.load(open(os.path.join(path, 'data_normalized.gxdb'), 'rb'))

    conf = json.load(open(os.path.join(path, 'conf.json'), 'rb'))
    init_params = {'data': data, 'data_normalized': data_normalized, 'spark_context': sc,
                   'global_max': conf['global_max'], 'global_min': conf['global_min']}
    db = genex_database(**init_params)

    db.set_clusters(db.get_sc().pickleFile(os.path.join(path, 'clusters.gxdb/*')))
    db.set_conf(conf)

    return db


class genex_database:
    """
    Genex Database

    Init parameters
    data
    data_normalized
    scale_funct
    """

    def __init__(self, **kwargs):
        """

        :param kwargs:
        """
        self.data = kwargs['data']
        self.data_normalized = kwargs['data_normalized']
        self.sc = kwargs['spark_context']
        self.cluster_rdd = None

        self.conf = {'build_conf': None,
                     'global_max': kwargs['global_max'],
                     'global_min': kwargs['global_min']}

    def set_conf(self, conf):
        self.conf = conf

    def set_clusters(self, clusters):
        self.cluster_rdd = clusters

    def get_sc(self):
        return self.sc

    def build(self, similarity_threshold: float, dist_type: str = 'eu', loi: slice = None, verbose: int = 1,
              _batch_size=None):
        """

        :param loi: default value is none, otherwise using slice notation [start, stop: step]
        :param similarity_threshold:
        :param dist_type:
        :param verbose:
        :return:
        """
        _validate_gxdb_build_arguments(locals())
        start, end = _process_loi(loi)
        # update build configuration
        self.conf['build_conf'] = {'similarity_threshold': similarity_threshold,
                                   'dist_type': dist_type,
                                   'loi': (start, end)}

        # validate and save the loi to gxdb class fields
        # distribute the data
        input_rdd = self.sc.parallelize(self.data_normalized, numSlices=self.sc.defaultParallelism)
        # partition_input = input_rdd.glom().collect() #  for debug purposes
        # Grouping the data
        # group = _group_time_series(input_rdd.glom().collect()[0], start, end) # for debug purposes
        group_rdd = input_rdd.mapPartitions(
            lambda x: _group_time_series(time_series=x, start=start, end=end), preservesPartitioning=True)
        # group_partition = group_rdd.glom().collect()  # for debug purposes

        # Cluster the data with Gcluster
        # cluster = _cluster_groups(groups=group_rdd.glom().collect()[0], st=similarity_threshold,
        #                           dist_type=dist_type, verbose=1)  # for debug purposes
        cluster_rdd = group_rdd.mapPartitions(lambda x: _cluster_groups(
            groups=x, st=similarity_threshold, dist_type=dist_type, log_level=verbose)).cache()
        # cluster_partition = cluster_rdd.glom().collect()  # for debug purposes

        cluster_rdd.collect()

        self.cluster_rdd = cluster_rdd

    def query_brute_force(self, query: Sequence, best_k: int):
        dist_type = self.conf.get('build_conf').get('dist_type')

        query.fetch_and_set_data(self.data_normalized)
        input_rdd = self.sc.parallelize(self.data_normalized, numSlices=self.sc.defaultParallelism)

        start, end = self.conf.get('build_conf').get('loi')
        slice_rdd = input_rdd.mapPartitions(
            lambda x: _slice_time_series(time_series=x, start=start, end=end), preservesPartitioning=True)

        dist_rdd = slice_rdd.map(lambda x: (sim_between_seq(query, x, dist_type=dist_type), x))

        candidate_list = dist_rdd.collect()
        candidate_list.sort(key=lambda x: x[0])

        query_result = candidate_list[:best_k]
        return query_result

    def save(self, path: str):
        """
        The save method saves the databse onto the disk.
        :param path: path to save the database to
        :return:
        """
        if os.path.exists(path):
            print('Path ' + path + ' already exists, overwriting...')
            shutil.rmtree(path)
            os.makedirs(path)

        # save the clusters if the db is built
        if self.cluster_rdd is not None:
            self.cluster_rdd.saveAsPickleFile(os.path.join(path, 'clusters.gxdb'))

        # save data files
        pickle.dump(self.data, open(os.path.join(path, 'data.gxdb'), 'wb'))
        pickle.dump(self.data_normalized, open(os.path.join(path, 'data_normalized.gxdb'), 'wb'))

        # save configs
        with open(path + '/conf.json', 'w') as f:
            json.dump(self.conf, f, indent=4)

    def is_id_exists(self, sequence: Sequence):
        return sequence.seq_id in dict(self.data).keys()

    def _get_data_normalized(self):
        return self.data_normalized

    def query(self, query: Sequence, best_k: int, exclude_same_id: bool = False, overlap: float = 1.0,
              _lb_opt_repr: str = 'none',
              _lb_opt_cluster: str = 'bsf'):
        """

        :param _lb_opt_cluster: lbh, bsf, lbh_bst, none
        :param _lb_opt_repr: lbh, none
        :param overlap:
        :param query:
        :param best_k:
        :param exclude_same_id:
        :return:
        """
        _validate_gxdb_query_arguments(locals())

        query.fetch_and_set_data(self._get_data_normalized())
        query = self.sc.broadcast(query)

        data_normalized = self.sc.broadcast(self._get_data_normalized())

        st = self.conf.get('build_conf').get('similarity_threshold')
        dist_type = self.conf.get('build_conf').get('dist_type')

        # for debug purposes
        a = _query_partition(cluster=self.cluster_rdd.glom().collect()[0], q=query, k=best_k, data_normalized=data_normalized, dist_type=dist_type,
                             _lb_opt_cluster=_lb_opt_cluster, _lb_opt_repr=_lb_opt_repr,
                             exclude_same_id=exclude_same_id, overlap=overlap,
                             )
        query_rdd = self.cluster_rdd.mapPartitions(
            lambda x:
            _query_partition(cluster=x, q=query, k=best_k, data_normalized=data_normalized, dist_type=dist_type,
                             _lb_opt_cluster=_lb_opt_cluster, _lb_opt_repr=_lb_opt_repr,
                             exclude_same_id=exclude_same_id, overlap=overlap,
                             )
        )
        aggre_query_result = query_rdd.collect()
        heapq.heapify(aggre_query_result)
        best_matches = []

        for i in range(best_k):
            best_matches.append(heapq.heappop(aggre_query_result))

        return best_matches



def _isOverlap(seq1: Sequence, seq2: Sequence, overlap: float) -> bool:
    if seq1.seq_id != seq2.seq_id:  # overlap does NOT matter if two seq have different id
        return True
    else:
        of = _calculate_overlap(seq1, seq2)
        return _calculate_overlap(seq1, seq2) >= overlap


def _calculate_overlap(seq1, seq2) -> float:
    if seq2.end > seq1.end and seq2.start >= seq1.start:
        return (seq1.end - seq2.start + 1) / (seq2.end - seq1.start + 1)
    elif seq1.end > seq2.end and seq1.start >= seq2.start:
        return (seq2.end - seq1.start + 1) / (seq1.end - seq2.start + 1)
    if seq2.end >= seq1.end and seq2.start > seq1.start:
        return (seq1.end - seq2.start + 1) / (seq2.end - seq1.start + 1)
    elif seq1.end >= seq2.end and seq1.start > seq2.start:
        return (seq2.end - seq1.start + 1) / (seq1.end - seq2.start + 1)

    elif seq1.end > seq2.end and seq2.start >= seq1.start:
        return len(seq2) / len(seq1)
    elif seq2.end > seq1.end and seq1.start >= seq2.start:
        return len(seq1) / len(seq2)
    elif seq1.end >= seq2.end and seq2.start > seq1.start:
        return len(seq2) / len(seq1)
    elif seq2.end >= seq1.end and seq1.start > seq2.start:
        return len(seq1) / len(seq2)

    elif seq2.start > seq1.end or seq1.start > seq2.end:  # does not overlap at all
        return 0.0
    else:
        print(seq1)
        print(seq2)
        raise Exception('FATAL: sequence 100% overlap, please report the bug')
