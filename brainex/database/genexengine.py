import heapq
import json
import math
import os
import pickle
import random
import statistics
from statistics import mode
from logging import warning

import numpy as np
import shutil

from pyspark.rdd import RDD
from scipy.spatial.distance import cityblock
from scipy.spatial.distance import euclidean
from scipy.spatial.distance import chebyshev

from brainex.classes.Sequence import Sequence
from brainex.op.query_op import _query_partition, sim_between_array
from brainex.utils.spark_utils import _cluster_with_spark, _query_bf_spark, _broadcast_kwargs, _destory_kwarg_bc, \
    _build_piecewise_spark, _query_paa_spark, _query_sax_spark, _query_piecewise_spark
from brainex.utils.utils import _validate_gxdb_build_arguments, _process_loi, _validate_gxe_query_arguments, _isOverlap, \
    flatten, process_loi_query, _min_max_normalize_single, \
    _inverse_min_max_normalize_single
from brainex.utils.context_utils import _multiprocess_backend

from brainex.utils.mutiprocess_utils import _cluster_multi_process, _query_bf_mp, _query_mp


def eu_norm(x, y):
    return euclidean(x, y) / np.sqrt(len(x))


def ma_norm(x, y):
    return cityblock(x, y) / len(x)


def ch_norm(x, y):
    return chebyshev(x, y)


def min_norm(x, y):
    return chebyshev(x, y)


dt_func_dict = {'eu': eu_norm,
                'ma': ma_norm,
                'ch': ch_norm,
                'min': min_norm
                }
dt_pnorm_dict = {'eu': 2,
                 'ma': 1,
                 'ch': math.inf,
                 'min': math.inf}


class GenexEngine:
    """
    Genex Engine

    Init parameters
    data_original
    data_normalized
    scale_funct
    """

    def __init__(self, **kwargs):
        """

        :param kwargs:
        """
        self.data_raw = kwargs['data_raw']
        self.data_original = kwargs['data_original']
        self.data_normalized = kwargs['data_normalized']
        self.mp_context = kwargs['mp_context']
        self.clusters = None
        self.subsequences = None
        self.subsequences_paa = None
        self.subsequences_sax = None
        self.cluster_meta_dict = None
        if 'conf' in kwargs.keys():
            self.conf = kwargs['conf']
        else:
            self.conf = {'global_max': kwargs['global_max'],
                         'global_min': kwargs['global_min'],
                         'backend': kwargs['backend'],
                         'has_uuid': kwargs['has_uuid'],
                         'seq_dim': kwargs['seq_dim']}
        self.build_conf = None
        self.bf_query_buffer = dict()

        self._data_normalized_bc = None
        self.feature_num = len(self.data_normalized[0][0])

    def __del__(self):
        self.stop()
        del self

    def __set_conf(self, conf):
        self.conf = conf

    def _set_clusters(self, clusters):
        self.clusters = clusters

    def get_mp_context(self):
        return self.mp_context

    def get_num_ts(self):
        return len(self.data_original)

    def set_build_conf(self, build_conf: dict):
        self.build_conf = build_conf

    def is_seq_exist(self, seq: Sequence):
        try:
            seq.fetch_data(self.data_normalized)
        except KeyError:
            return False
        return True

    def check_dim(self, seq):
        """
        raises exception if the dimension of the given sequences' does not match the sequence dim of the stored dim
        :param seq: the sequence of which to check against
        """
        seq_shape = seq.data.shape if type(seq) is Sequence else np.asarray(seq).shape
        try:
            assert len(seq_shape) == 2 or len(seq_shape) == 1
            if len(seq_shape) == 2:
                assert seq_shape[-1] == self.conf['seq_dim']
        except AssertionError:
            raise Exception(
                'Error checking dimension, expected: (' + str(self.conf['seq_dim']) + ',n), got ' + str(seq_shape))

    def build(self, st: float, dist_type: str = 'eu', loi=None, verbose: int = 1, _group_only=False, _use_dss=True,
              _use_dynamic=False):
        """
        Groups and clusters the time series set

        if the number of time series is less than the number of works, the spark version will use sdg to speed up
        grouping
        :param _use_dss:
        :param _group_only:
        :param st: The upper bound of the similarity value between two time series (Value must be
                                      between 0 and 1)
        :param dist_type: Distance type used for similarity calculation between sequences
        :param loi: default value is none, otherwise using slice notation [start, stop: step]
        :param verbose: Print logs when grouping and clustering the data_original
        :param batch_size:
        :param _is_cluster: Decide whether time series data_original is clustered or not

        """
        _validate_gxdb_build_arguments(locals())
        start, end = _process_loi(loi, max_len=self.get_max_seq_len())
        # update build configuration
        self.build_conf = {'similarity_threshold': st,
                           'dist_type': dist_type,
                           'loi': (start, end),
                           'piecewise': tuple()}

        # determine the distance calculation function
        try:
            dist_func = dt_func_dict[dist_type]
            pnorm = dt_pnorm_dict[dist_type]
        except KeyError:
            raise Exception('Unknown distance type: ' + str(dist_type))

        if self.is_using_spark():  # If using Spark backend
            self._data_normalized_bc = self.mp_context.broadcast(self.data_normalized)
            dn = self._data_normalized_bc
            self.subsequences, self.clusters, self.cluster_meta_dict = \
                _cluster_with_spark(self.mp_context,
                                    self.data_normalized,
                                    dn,
                                    start, end, st, dist_func, pnorm,
                                    verbose, _group_only, _use_dss, _use_dynamic)
        else:
            self.subsequences, self.clusters, self.cluster_meta_dict = \
                _cluster_multi_process(self.mp_context,
                                       self.data_normalized,
                                       start, end, st, dist_func,
                                       pnorm,
                                       verbose, _use_dynamic)

    def get_cluster(self, rprs: Sequence):
        length = None

        for k, v in self.cluster_meta_dict.items():
            if rprs in v.keys():
                length = k
                break

        if length is None:
            raise ValueError('get_cluster: Couldn\'t find the representative in the cluster, please check the input.')

        target_cluster_rdd = self.clusters.filter(lambda x: rprs in x[1].keys()).collect()
        cluster = target_cluster_rdd[0][1].get(rprs)

        return cluster

    def get_num_subsequences(self):
        try:
            assert self.clusters is not None
        except AssertionError:
            raise Exception('get_num_subsequences: the database must be build before calling this function')
        return self.subsequences.count() if self.is_using_spark() else len(self.subsequences)

    def query_brute_force(self, query: Sequence, best_k: int, _use_cache: bool = True, _piecewise: str = None, _use_built_piecewise: bool=True):
        """
        Retrieve best k matches for query sequence using Brute force method

        :param _use_cache:
        :param query: Sequence being queried
        :param best_k: Number of best matches to retrieve for the given query
        :param _piecewise: number of segments of time series reduction while applying piecewise aggregation approximation representative

        :return: a list containing best k matches for given query sequence
        """
        query = self._process_query(query)
        dist_type = self.build_conf.get('dist_type')
        dt_index = dt_pnorm_dict[dist_type]

        candidate_list = self._qbf(query, dt_index, best_k, _use_cache, _piecewise, _use_built_piecewise)
        rtn = candidate_list[:best_k]
        if _piecewise: # calculate the true DTW distance (not piecewise approximated)
            rtn = [(sim_between_array(self.get_seq_data(x[1]), query.data, pnorm=dt_index), x[1]) for x in rtn]
            rtn.sort(key=lambda x: x[0])
        return rtn

    def _qbf(self, query, dt_index, best_k, use_cache, piecewise: str, _use_built_piecewise):

        dn = self._data_normalized_bc if self.is_using_spark() else self.data_normalized
        candidate_list = self.check_bf_query_cache(query, best_k=best_k) if use_cache else None  # TODO paa cache check

        if not candidate_list:  # there is no cached brute force result
            if self.is_using_spark():
                query_data = query.data
                if query_data is None:
                    query_data = self.get_seq_data(query)
                if not piecewise:
                    candidate_list = _query_bf_spark(query, self.subsequences, dt_index, data_list=dn)
                elif piecewise == 'paa':
                    if _use_built_piecewise:
                        try:
                            assert 'paa' in self.build_conf['piecewise']
                        except AssertionError:
                            raise Exception('genexengine: must build_piece with the mode paa before querying with it')
                        candidate_list = _query_paa_spark(query, self.subsequences_paa, dt_index,
                                                          self.build_conf['n_segment'])
                    else:
                        candidate_list = _query_piecewise_spark(query_data, self.subsequences, dt_index,  data_list=dn,
                                                                piecewise=piecewise, n_segment=self.build_conf['n_segment'])
                elif piecewise == 'sax':
                    if _use_built_piecewise:
                        try:
                            assert 'sax' in self.build_conf['piecewise']
                        except AssertionError:
                            raise Exception('genexengine: must build_piece with the mode sax before querying with it')
                        candidate_list = _query_sax_spark(query, self.subsequences_sax, dt_index,
                                                          self.build_conf['n_segment'])
                    else:
                        candidate_list = _query_piecewise_spark(query_data, self.subsequences, dt_index,  data_list=dn,
                                                                piecewise=piecewise, n_segment=self.build_conf['n_segment'])
            else:
                candidate_list = _query_bf_mp(query, self.mp_context, self.subsequences, dt_index, piecewise,
                                              data_list=dn)
        else:
            print('bf_query: using buffered bf results')
        if use_cache:
            self.bf_query_buffer[query] = candidate_list

        candidate_list.sort(key=lambda x: x[0])
        return candidate_list

    def check_bf_query_cache(self, query, best_k):
        key = query
        try:
            return self.bf_query_buffer[key] if len(self.bf_query_buffer[key]) >= best_k else None
        except KeyError:
            return None

    def set_piecewise_segment(self, n_segment: int):
        self.build_conf['n_segment'] = n_segment

    def reset_mp(self, use_spark, **kwargs):
        self.stop()
        self.mp_context = _multiprocess_backend(use_spark, **kwargs)

    def build_piecewise(self, mode: str, n_segment: int = 3, _dummy_slicing: bool = False):
        """
        preprocess function that must be run before calling PAA query
        must be run after build, because the subsequences are otherwise empty
        creates PAA compressed version of all the subsequences
        :param resize_by:
        """
        if self.subsequences is None:  # must be run after building
            raise Exception \
                ('GenexEngine: engine not build, GenexEngine.build(...) must be called prior to this function')
        if self.is_using_spark():
            dn = self._data_normalized_bc if self.is_using_spark() else self.data_normalized
            start, end = self.build_conf.get('loi')
            piecewise_kv_rdd = _build_piecewise_spark(self.subsequences, mode, n_segment, data_list=dn,
                                                      _dummy_slicing=_dummy_slicing, _sc=self.mp_context, _start=start,
                                                      _end=end)

        else:
            # _build_paa(self.mp_context)
            raise Exception('GenexEngine: prepare PAA is not implemented for non-spark version currently.')
        self.build_conf['n_segment'] = n_segment
        if mode == 'paa':
            self.subsequences_paa = piecewise_kv_rdd
        elif mode == 'sax':
            self.subsequences_sax = piecewise_kv_rdd
        self.build_conf['piecewise'] = tuple(set([*self.build_conf['piecewise'], mode]))

    # def group_sequences(self):
    #     """
    #     helper function to monitor memory usage
    #     """
    #     input_rdd = self.mp_context.parallelize(self.data_normalized, numSlices=self.mp_context.defaultParallelism)
    #     # process all possible length
    #     start, end = _process_loi(None)
    #
    #     slice_rdd = input_rdd.mapPartitions(
    #         lambda x: _slice_time_series(time_series=x, start=start, end=end), preservesPartitioning=True)
    #
    #     return slice_rdd.collect()

    def get_random_seq_of_len(self, sequence_len, seed, with_data=False, normalize=True):
        if sequence_len < 1:
            warning('Genex Engine: cannot give sequences with length less than 1, setting sequence_len to 1')
            sequence_len = 1
        random.seed(seed)
        filtered = [x for x in self.data_normalized if len(x[1]) >= sequence_len]
        try:
            assert len(filtered) > 0
        except AssertionError:
            raise Exception('get_random_seq_of_len: given length does not exist in the database. Given ' + str(sequence_len) +
                            ', Max len is ' + str(self.get_max_seq_len()) +
                            '\nIf you think this is an implementation error, please report to the Repository as an issue.')
        target = random.choice(filtered)
        start = random.randint(0, len(target[1]) - sequence_len)
        seq = Sequence(target[0], start, start + sequence_len - 1)

        try:
            assert len(seq.fetch_data(self.data_normalized)) == sequence_len
        except AssertionError:
            raise Exception(
                'get_random_seq_of_len: given length does not have corresponding data. If you think this is '
                'an implementation error, please report to the Repository as an issue.')

        if with_data:
            seq.fetch_and_set_data(self.data_normalized) if normalize else seq.fetch_and_set_data(self.data_original)
        return seq

    def get_seqs_of_len(self, seq_len):
        if self.is_using_spark():
            rtn = self.subsequences.filter(lambda x: len(x) == seq_len).collect()
        else:
            rtn = None
            print('Warning: this feature is not yet implemented for none-spark version')
        return rtn

    def get_subsequences(self):
        return self.subsequences.collect() if self.is_using_spark() else self.subsequences

    def get_norm_ts_list(self):
        return [Sequence(seq_id=x[0], start=0, end=len(x[1]) - 1, data=x[1]) for x in self.data_normalized]

    def get_seq_length_list(self):
        return [len(ts[1]) for ts in self.data_normalized]

    def get_data_size(self):
        """
        get the number of data points in the given dataset
        :return: the number of floating-point data points in the original dataset, excluding the features.
        """
        return np.sum(self.get_seq_length_list())

    def save(self, path: str):
        """
        The save method saves the database onto the disk.
        :param path: path to save the database to

        """
        if os.path.exists(path):
            print('Path ' + path + ' already exists, overwriting...')
            shutil.rmtree(path)
            os.makedirs(path)
        else:
            os.makedirs(path)

        # save the clusters if the db is built
        if self.clusters is not None:
            self._save_cluster(path)
            pickle.dump(self.cluster_meta_dict, open(os.path.join(path, 'cluster_meta_dict.gxe'), 'wb'))
            with open(path + '/build_conf.json', 'w') as f:
                json.dump(self.build_conf, f, indent=4)

        # save data_original files
        pickle.dump(self.data_original, open(os.path.join(path, 'data_original.gxe'), 'wb'))
        pickle.dump(self.data_normalized, open(os.path.join(path, 'data_normalized.gxe'), 'wb'))
        self.data_raw.to_csv(os.path.join(path, 'data_raw.csv'))

        # save configs
        with open(path + '/conf.json', 'w') as f:
            conf_ = self.conf.copy()
            # cast to int to avoid json serialization issue in bxe.save()
            conf_['global_max'] = int(conf_['global_max'])
            conf_['global_min'] = int(conf_['global_min'])
            json.dump(conf_, f, indent=4)

    def _save_cluster(self, path):
        if self.is_using_spark():
            self.clusters.saveAsPickleFile(os.path.join(path, 'clusters.gxe'))
            self.subsequences.saveAsPickleFile(os.path.join(path, 'subsequences.gxe'))
        else:
            pickle.dump(self.clusters, open(os.path.join(path, 'clusters.gxe'), 'wb'))
            pickle.dump(self.subsequences, open(os.path.join(path, 'subsequences.gxe'), 'wb'))

    def load_cluster(self, path):
        if self.is_using_spark():
            self._set_clusters(self.get_mp_context().pickleFile(os.path.join(path, 'clusters.gxe/*')))
            self._set_subsequences(self.get_mp_context().pickleFile(os.path.join(path, 'subsequences.gxe/*')))
        else:
            self._set_clusters(pickle.load(open(os.path.join(path, 'clusters.gxe'), 'rb')))
            self._set_subsequences(pickle.load(open(os.path.join(path, 'subsequences.gxe'), 'rb')))

    def is_id_exists(self, sequence: Sequence):
        return sequence.seq_id in dict(self.data_original).keys()

    def _get_data_normalized(self):
        return self.data_normalized

    def _set_subsequences(self, subsequences):
        self.subsequences = subsequences

    def is_using_spark(self):
        return self.conf['backend'] == 'spark'

    def get_seq_data(self, seq: Sequence, normalize=True):
        if not self.is_id_exists(seq):
            raise Exception('Give sequence is not in the dataset')
        return seq.fetch_data(self.data_original) if not normalize else seq.fetch_data(self.data_normalized)

    def set_seq_data(self, seq: Sequence, normalize=False):
        if not self.is_id_exists(seq):
            raise Exception('Give sequence is not in the dataset')
        seq.fetch_and_set_data(self.data_original) if not normalize else seq.fetch_data(self.data_normalized)

    def query(self, query, best_k: int,
              id_filter=None, filter_mode=None, loi=None,
              exclude_same_id: bool = False, overlap: float = 1.0,
              _lb_opt: bool = False, _ke=None, _radius: int = 1, _ke_factor: int = 1):
        """
        Find best k matches for given query sequence using Distributed Genex method

        :param filter_mode: must be any or all
        :param loi:
        :param id_filter:
        :param _ke_factor:
        :param _lb_opt:
        :param query:
        :param _radius:
        :param _ke:
        :param: query: Sequence to be queried
        :param best_k: Number of best matches to retrieve
        :param exclude_same_id: Whether to exclude query sequence in the retrieved matches
        :param overlap: Value for overlapping parameter (Must be between 0 and 1 inclusive), this is to say any pair in
        the query result will no overlap more than <overlap> percent

        :return: a list containing k best matches for given query sequence
        """
        _validate_gxe_query_arguments(locals())
        if loi:
            start, end = process_loi_query(loi, self.build_conf.get('loi'))
            loi = (start, end)
            pass
        query = self._process_query(query)

        _ke = self._process_ke(_ke_factor, best_k)
        st = self.build_conf.get('similarity_threshold')
        dist_type = self.build_conf.get('dist_type')

        dn = self._data_normalized_bc if self.is_using_spark() else self.data_normalized
        q = self.mp_context.broadcast(query) if self.is_using_spark() else query
        # order of this kwargs MUST be perserved in accordance to genex.op.query_op._query_partition

        query_args = {'q': q, 'k': best_k, 'ke': _ke, 'data_normalized': dn, 'pnorm': dt_pnorm_dict[dist_type],
                      'lb_opt': _lb_opt, 'exclude_same_id': exclude_same_id, 'radius': _radius,
                      'st': st, 'overlap': overlap,
                      'id_filter': id_filter, 'filter_mode': filter_mode, 'loi': loi
                      }
        best_matches = []
        while len(best_matches) < best_k:
            if self.is_using_spark():  # The only place in query where it checks if is using Spark
                query_rdd: RDD = self.clusters.mapPartitions(
                    lambda c: _query_partition(**query_args, cluster=c, prev_matches=best_matches))
                candidates = query_rdd.collect()
            else:
                candidates = _query_mp(self.mp_context, self.clusters, **query_args)
            #### testing distribute query vs. one-core query
            # result_distributed = query_rdd.collect()
            # result_distributed.sort(key=lambda x: x[0])
            # result_distributed = result_distributed[:10]
            # result_one_core = a
            # result_one_core.sort(key=lambda x: x[0])
            # result_one_core = result_one_core[:10]
            # is_same = np.equal(result_distributed, result_one_core)

            # apply overlap
            # note that we are using k here
            # while len(c_dist_list) > 0 and len(query_result) < k:
            #     c_dist = heapq.heappop(c_dist_list)
            #     if overlap == 1.0 or exclude_same_id:
            #         query_result.append(c_dist)
            #     else:
            #         if not any(_isOverlap(c_dist[1], prev_match[1], overlap) for prev_match in
            #                    query_result):  # check for overlap against all the matches so far
            #             query_result.append(c_dist)
            # return query_result
            heapq.heapify(candidates)
            while len(candidates) > 0:
                if len(best_matches) >= best_k:
                    break
                this_c = heapq.heappop(candidates)
                if overlap == 1.0:  # needless to consider overlap if same if as already been excluded
                    best_matches.append(this_c)
                else:  # if consider overlap
                    if not any(_isOverlap(this_c[1], prev_match[1], overlap) for prev_match in best_matches):
                        best_matches.append(this_c)
                pass
        if self.is_using_spark():
            q.destroy()
            query_rdd.unpersist()
        return best_matches

    def get_num_clusters(self):
        return len(flatten(self.cluster_meta_dict.values()))

    def query_on_batch(self, query: Sequence, best_k: int, exclude_same_id: bool = False, overlap: float = 1.0,
                       _lb_opt: bool = False, _ke=None, _radius: int = 1):
        pass

    def query_bf_on_batch(self):
        pass

    def _process_query(self, query):
        if type(query) is Sequence:
            if query.data is None:
                query.fetch_and_set_data(self._get_data_normalized())
            else:
                pass
        else:  # if query is an time series outside of the original dataset, make it into a Sequence object.
            try:
                query = Sequence(seq_id=('outside sequence', 0), start=0, end=len(query), data=np.asarray(query))
            except TypeError as e:
                raise Exception('Unsupported query type: the query must be an iterable consisted of numbers')
        self.check_dim(query)
        return query

    def _process_ke(self, ke_factor, best_k):
        _ke = best_k * ke_factor
        if _ke > self.get_num_subsequences():
            raise Exception('query: _ke cannot be smaller than the number of subsequences in the database.')
        return _ke

    def set_cluster_meta_dict(self, cluster_meta_dict):
        self.cluster_meta_dict = cluster_meta_dict

    def get_max_seq_len(self):
        return max([len(x[1]) for x in self.data_normalized])

    def stop(self):
        """
        Must be called before removing a gxe object
        """
        if self.is_using_spark():
            self.mp_context.stop()
        else:
            self.mp_context.terminate()
            self.mp_context.close()

    def motif_all_length(self, absolute):
        """
        :return dict:
            sequence_len -> list of [representatives,
                                    number of represented sequence (int) if absolute is True /
                                    relative representativeness (float) if absolute is False]
        """
        pattern = dict()  # seq_len (int) -> [most represented representative (sequence), number of represented sequences]
        for seq_len, clusters in self.cluster_meta_dict.items():
            subseq_num_of_len = np.sum(list(clusters.values()))
            cluster_list = list(clusters.items())  # repr (sequence) -> number of represented sequences (int)
            cluster_list.sort(key=lambda x: x[1], reverse=True)

            # change the 'number of represented sequences' to representativeness
            cluster_list = [(x[0], x[1] / subseq_num_of_len) for x in cluster_list] if not absolute else cluster_list
            pattern[seq_len] = cluster_list
        return pattern

    def motif(self, k, overlap: float = 1.0, absolute=True):
        """
        :param k:
        :param absolute: use the absolute or the relative representativeness
        :return list of tuples:
            [common pattern/most represented representative (Sequence),
            number of represented sequence (int) if absolute is True /
            relative representativeness (float) if absolute is False]
        """
        pattern_list = flatten(list(self.motif_all_length(absolute).values()))
        pattern_list.sort(key=lambda x: x[1], reverse=True)

        if overlap == 1.0:
            return pattern_list[:k]
        else:
            rtn = []
            while len(pattern_list) > 0 and len(rtn) < k:
                cur = pattern_list.pop(0)
                truth = [_isOverlap(cur[0], x[0], overlap) for x in rtn]
                if not any(_isOverlap(cur[0], x[0], overlap) for x in
                           rtn):  # check for overlap against all the matches so far
                    rtn.append(cur)
            return rtn

    def predice_label_knn(self, query, k, label_index, verbose=0):
        """
        will return None if the voting result result in a tie
        :param query:
        :param k:
        :param label_index: which label in the time series id to predict
        """
        try:
            assert label_index < self.feature_num - 1
        except AssertionError as e:
            raise Exception('Given label index is out of bound of the number of features in the dataset')
        label_index = label_index + 1 if self.conf['has_uuid'] else label_index
        kn = self.query(query, k, exclude_same_id=True)
        kn_labels = [n[1].seq_id[label_index] for n in kn]
        try:
            res = mode(kn_labels)
        except statistics.StatisticsError:
            return None
        if verbose == 1:
            print(
                str(kn_labels.count(res)) + ' out of ' + str(len(kn_labels)) + ' voted positive for label:' + str(res))
        return res

    def predite_label_knn_on_batch(self):
        pass

    def normalize(self, array):
        """
        normalize a sequence on the same scale as the loaded dataset, this sequence can later be used to query.
        """
        return _min_max_normalize_single(array, global_max=self.conf['global_max'], global_min=self.conf['global_min'])

    def inverse_normalize(self, array):
        return _inverse_min_max_normalize_single(array, global_max=self.conf['global_max'], global_min=self.conf['global_min'])


def _is_overlap(seq1: Sequence, seq2: Sequence, overlap: float) -> bool:
    """
     Check for overlapping between two time series sequences

    :param seq1: Time series Sequence
    :param seq2: Time series Sequence
    :param overlap: Value for overlap (must be between 0 and 1 inclusive)

    :return: boolean value based on whether two sequences overlap more or less than given overlap parameter
    """
    if seq1.seq_id != seq2.seq_id:  # overlap does NOT matter if two seq have different id
        return True
    else:
        return _calculate_overlap(seq1, seq2) >= overlap


def _calculate_overlap(seq1, seq2) -> float:
    """
    Calculate overlap between two time series sequence

    :param seq1: Time series sequence
    :param seq2: Time series sequence

    :return: overlap value between two sequences
    """
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



