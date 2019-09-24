# -*- coding: utf-8 -*-
"""
Created on Fri Aug 30 12:02:42 2019

@author: Andrew Wentzel
"""
from numpy.random import seed
seed(1)
from tensorflow.compat.v1 import set_random_seed
set_random_seed(2)

from PatientSet import PatientSet
from Constants import Constants
import Metrics
from analysis import *
from Models import *
from copy import copy
import numpy as np
import pandas as pd
from dependencies.Boruta import BorutaPy
from sklearn.feature_selection import mutual_info_classif, f_classif, SelectPercentile, SelectKBest
from sklearn.model_selection import cross_validate, cross_val_predict, LeaveOneOut
from sklearn.metrics import accuracy_score, recall_score, roc_auc_score, roc_curve, f1_score
from sklearn.naive_bayes import BernoulliNB, ComplementNB, GaussianNB, MultinomialNB
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import OneHotEncoder, QuantileTransformer, PowerTransformer
from sklearn.cluster import AgglomerativeClustering
from sklearn.tree import DecisionTreeClassifier
from sklearn.discriminant_analysis import QuadraticDiscriminantAnalysis
from sklearn.ensemble import VotingClassifier, ExtraTreesClassifier, RandomForestClassifier, AdaBoostClassifier
from sklearn.svm import SVC
from dependencies.NCA import NeighborhoodComponentsAnalysis
from sklearn.base import BaseEstimator, ClassifierMixin
from collections import namedtuple, OrderedDict
from imblearn import under_sampling, over_sampling, combine
from scipy.special import softmax
from sklearn.metrics import silhouette_score
from scipy.stats import kruskal

from time import time
from datetime import datetime

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)
#warnings.filterwarnings("ignore", category=RuntimeWarning)

class MetricLearningClassifier(BaseEstimator, ClassifierMixin):

    def __init__(self, n_components = 'auto',
                 random_state = 1,
                 resampler = None,
                 use_softmax = True):
        self.n_components = n_components
        if n_components is not 'auto':
            self.transformer = NeighborhoodComponentsAnalysis(n_components = n_components)
        self.group_parameters = namedtuple('group_parameters', ['means', 'inv_covariance', 'max_dist'])
        self.resampler = resampler
        self.use_softmax = use_softmax

    def get_optimal_components(self, x, y):
        n_components = x.shape[1]
        def get_score():
            nca = NeighborhoodComponentsAnalysis(n_components = n_components)
            nca.fit(x,y)
            return silhouette_score(nca.transform(x), y), nca
        score, nca = get_score()
        while True:
            if n_components <= 2:
                return nca
            n_components -= 1
            new_score, new_nca = get_score()
            if new_score > 1.1*score:
                score = new_score
                nca = new_nca
            else:
                return nca

    def fit(self, x, y):
        if self.n_components == 'auto':
            self.transformer = self.get_optimal_components(x, y)
        self.transformer.fit(x, y)
        self.groups = OrderedDict()
        if self.resampler is not None:
            xtemp, ytemp = self.resampler.fit_resample(x,y)
            if len(np.unique(ytemp)) == len(np.unique(y)):
                x = xtemp
                y = ytemp
        for group in np.unique(y):
            self.groups[group] = self.group_params(x, y, group)

    def group_params(self, x, y, group):
        targets = np.argwhere(y == group).ravel()
        x_target = self.transformer.transform(x[targets])
        fmeans = x_target.mean(axis = 0)
        inv_cov = np.linalg.pinv(np.cov(x_target.T))
        train_dists = self.mahalanobis_distances(x, self.group_parameters(fmeans, inv_cov, 0))
        parameters = self.group_parameters(fmeans, inv_cov, train_dists.max())
        return parameters

    def mahalanobis_distances(self, x, group):
        x_offset = self.transformer.transform(x) - group.means
        left_term = np.dot(x_offset, group.inv_covariance)
        mahalanobis = np.dot(left_term, x_offset.T).diagonal()
        return mahalanobis

    def predict_proba(self, x):
        all_distances = []
        for group_id, group_params in self.groups.items():
            distances = self.mahalanobis_distances(x, group_params)
            proximity = np.clip(1 - (distances/group_params.max_dist), 0.00001, 1)
            all_distances.append(proximity)
        output = np.hstack(all_distances).reshape(-1, len(self.groups.keys()))
        if self.use_softmax:
            output = softmax(output)
        else:
            output = output/output.sum(axis = 1).reshape(-1,1)
        return output

    def predict(self, x):
        labels = list(self.groups.keys())
        probs = self.predict_proba(self, x)
        max_probs =  np.argmax(probs, axis = 1).ravel()
        ypred = np.zeros(max_probs.shape).astype(np.dtype(labels[0]))
        for i in range(max_probs.shape[0]):
            ypred[i] = labels[max_probs[i]]
        return ypred[i]

    def fit_predict(self, x, y):
        self.fit(x,y)
        return self.predict(x)

class BayesWrapper(BaseEstimator, ClassifierMixin):

    def __init__(self, bayes = BernoulliNB(alpha = 0), n_categories = None):
        if n_categories is None:
            self.encoder = OneHotEncoder(categories = 'auto',
                                         sparse = False,
                                         handle_unknown = 'ignore')
        else:
            self.encoder = KBinsDiscretizer(n_bins = n_categories, encode = 'ordinal')
        self.bayes = bayes

    def fit(self, x, y):
        x = self.encoder.fit_transform(x)
        self.bayes.fit(x,y)
        return self

    def predict(self, x):
        xpred = self.encoder.transform(x)
        return self.bayes.predict(xpred)

    def predict_proba(self, x):
        xpred = self.encoder.transform(x)
        return self.bayes.predict_proba(xpred)

    def fit_predict(self, x, y):
        self.fit(x,y)
        return self.predict(x)



class RecallBasedModel:

    def __init__(self, model, recall_threshold = None, feature_selection = False):
        self.model = copy(model)
        self.recall_threshold = recall_threshold
        self.probability_threshold = None
        self.feature_selection = feature_selection

    def fit(self, x, y):
        self.get_feature_args(x,y)
        xfit = x[:, self.features_to_use]
        self.model.fit(xfit, y.ravel())
        if self.recall_threshold is not None:
            self.tune_threshold(xfit, y.ravel())

    def predict(self, x):
        xsubset = x[:, self.features_to_use]
        if self.probability_threshold is not None:
            probs = self.model.predict_proba(xsubset)[:,1].ravel()
            prediction = probs >= self.probability_threshold
        else:
            prediction = self.model.predict(xsubset)
        return prediction.astype('bool')

    def fit_predict(self, x, y):
        self.fit(x,y)
        return self.predict(x,y);

    def tune_threshold(self, x, y):
        ypred = self.model.predict_proba(x)
        sorted_scores = sorted(ypred[:,1], key = lambda x: -x)
        threshold_i = 0
        ythresh = ypred[:,1] >= sorted_scores[threshold_i]
        while recall_score(y, ythresh) < self.recall_threshold and threshold_i < len(sorted_scores) - 1:
            threshold_i += 1
            ythresh = ypred[:,1] >= sorted_scores[threshold_i]
        self.probability_threshold = sorted_scores[threshold_i]
        self.all_thresholds = sorted_scores

    def increment_threshold(self, increment = 1):
        current_index = self.all_thresholds.index(self.probability_threshold)
        new_index = np.clip(0, current_index + increment, len(self.all_thresholds) -1)
        self.probability_threshold = self.all_thresholds[new_index]

    def get_feature_args(self, x, y, percentile = 80, k = 40):
        if self.feature_selection == 'info':
            info_score = mutual_info_classif(x, y)
            self.features_to_use = np.argwhere(info_score > 0).ravel()
            if len(self.features_to_use) <= 1:
                self.features_to_use = np.argwhere(x.std(axis = 0) > 0).ravel()
        elif self.feature_selection == 'percentile':
            selector = SelectPercentile(percentile = percentile)
            selector.fit(x,y)
            self.features_to_use = np.argwhere(selector.get_support()).ravel()
        elif self.feature_selection == 'kbest':
            k = np.min([int(np.ceil(percentile*x.shape[1]/100)), k])
            selector = SelectKBest(k = k).fit(x,y)
            self.features_to_use = np.argwhere(selector.get_support()).ravel()
        else:
            self.features_to_use = np.argwhere(x.std(axis = 0) > 0).ravel()

    def __str__(self):
        string = str(self.model)
        string += '\n num features ' + str(len(self.features_to_use))
        string += '\n threshold ' + str(self.probability_threshold)
        string += '\n num_in_threshold: ' + str(self.all_thresholds.index(self.probability_threshold))
        return string + '\n'

class StackedClassifier:

    def __init__(self, default_models,
                 min_misses = 0,
                 recall_threshold = None,
                 feature_selection = False,
                 num_feature_splits = 2):
        self.gen_models = lambda: [RecallBasedModel(copy(m), recall_threshold, feature_selection) for m in default_models]
        self.min_misses = min_misses
        self.num_models = len(default_models)
        self.num_feature_splits = num_feature_splits

    def fit(self, x, y):
        models = []
        x_groups = self.split_features(x, y)
        for x_set in x_groups:
            model_set = self.gen_models()
            model_group = []
            for model in model_set:
                model.fit(x_set, y)
                model_group.append(model)
            models.append(model_group)
        self.models = models

    def predict(self, x, min_votes = None):
        min_votes = self.num_models if min_votes is None else min_votes
        x_groups= self.split_features(x)
        assert(len(x_groups) == len(self.models))
        predictions = []
        for group in range(len(x_groups)):
            model_set = self.models[group]
            x_set = x_groups[group]
            for model in model_set:
                ypred = model.predict(x_set)
                predictions.append(ypred.reshape(-1,1))
        predictions = np.hstack(predictions)
        ypred = predictions.sum(axis = 1) >= min_votes
        return ypred

    def fit_predict(self, x,y, min_votes = None):
        self.fit(x,y)
        return self.predict(x, min_votes)

    def split_features(self, x,y = None):
        #feature clusteirng or whatever here
        #just split if train is false, find grups if true
        if self.num_feature_splits <= 1:
            return [x]
        if y is not None:
            info_score = np.nan_to_num(mutual_info_classif(x, y))
            scores = np.argsort(-info_score)
            self.feature_group_args = [[] for g in range(self.num_feature_splits)]
            current_feature = 0
            while current_feature < x.shape[1]:
                for group in range(self.num_feature_splits):
                    self.feature_group_args[group].append(scores[current_feature])
                    current_feature += 1
                    if current_feature >= x.shape[1]:
                        break
#            self.feature_group_args = []
#            clusterer = AgglomerativeClustering(n_clusters = self.num_feature_splits)
#            features = x.transpose() #should I regularize here?
#            groups = clusterer.fit_predict(features)
#            for g in np.unique(groups):
#                args = np.argwhere(groups == g).ravel()
#                self.feature_group_args.append(args)
        x_groups = []
        for group_args in self.feature_group_args:
            x_groups.append(x[:, group_args])
        return x_groups

    def __repr__(self):
        string = ""
        for modelset in self.models:
            for model in modelset:
                string += str(model)
        return string

class IterativeClassifier:

    def __init__(self, default_models,
                 min_misses = 0,
                 recall_threshold = None,
                 feature_selection = False,
                 num_feature_splits = 1):
        self.gen_ensemble = lambda: StackedClassifier(default_models,
                                                      min_misses,recall_threshold,
                                                      feature_selection,
                                                      num_feature_splits)
        self.min_misses = min_misses
        self.num_models = len(default_models)
        self.num_feature_splits = num_feature_splits

    def fit(self, x, y):
        current_model = self.gen_ensemble()
        models = [current_model]
        y_pred = current_model.fit_predict(x,y)
        while not self.is_done(y, y_pred) and len(models) < 7:
            args = np.argwhere(y_pred == 0).ravel()
#            print(len(args))
            if len(args) < 5:
                break
            xsubset = x[args,:]
            ysubset = y[args]
            new_model = self.gen_ensemble()
            new_predict = new_model.fit_predict(xsubset, ysubset)
            new_args = np.argwhere(new_predict > 0)
            if len(new_args) <= 0:
                break
            y_pred[args[new_args]] = True
            models.append(new_model)
        self.models = models
#        print()

    def predict(self, x):
        y_pred = np.zeros((x.shape[0],))
        for model in self.models:
            valid = np.argwhere(model.predict(x) > 0).ravel()
            y_pred[valid] = 1
        return y_pred

    def predict_proba(self, x):
        y_pred = np.zeros((x.shape[0],))
        model_pos = 1
        for model in self.models:
            valid = np.argwhere(model.predict(x) > 0).ravel()
            y_pred[valid] = 1/model_pos
            model_pos += 1
        return y_pred

    def fit_predict(self, x, y):
        self.fit(x,y)
        return self.predict(x)

    def is_done(self, y, y_pred):
        false_predictions = np.argwhere(y_pred == 0).ravel()
        false_negatives = y[false_predictions].sum()
        return (false_negatives <= self.min_misses)

    def test(self, x, y):
        ypred = self.fit_predict(x, y)
        yproba = self.predict_proba(x)
        print('AUC score', roc_auc_score(y, yproba))
        print('recall ', recall_score(y, ypred))
        print('f1 ', f1_score(y, ypred))
        return yproba


def feature_matrix(db):
    discrete_dists = Metrics.discretize(-db.tumor_distances, n_bins = 15, strategy='uniform')
    t_volumes = np.array([np.sum([g.volume for g in gtvs]) for gtvs in db.gtvs]).reshape(-1,1)
    discrete_volumes = Metrics.discretize(t_volumes, n_bins = 15, strategy='uniform')

    discrete_sim = Metrics.augmented_sim(discrete_dists, Metrics.jaccard_distance)
#    predicted_doses = TreeKnnEstimator().predict_doses([discrete_sim], db)

    x = np.hstack([
        TreeKnnEstimator().predict_doses([discrete_sim], db),
        discrete_dists,
        discrete_volumes,
        db.prescribed_doses.reshape(-1,1),
        db.dose_fractions.reshape(-1,1),
        db.has_gtvp.reshape(-1,1),
        OneHotEncoder(sparse = False).fit_transform(db.lateralities.reshape(-1,1)),
        OneHotEncoder(sparse = False).fit_transform(db.subsites.reshape(-1,1)),
        OneHotEncoder(sparse = False).fit_transform(db.t_categories.reshape(-1,1)),
               ])
    return x

def cv_ensemble(features,
                toxicity,
                classifiers = None,
                num_feature_splits = 1,
                recall_threshold = .99,
                feature_selection = False,
                regularizer = None):
    if isinstance(features, PatientSet):
        features = feature_matrix(features)
    if classifiers is None:
        classifiers = [
                BernoulliNB(),
                LogisticRegression(solver = 'lbfgs',max_iter=3000)
                       ]
    ensemble = IterativeClassifier(classifiers,
                                   num_feature_splits = num_feature_splits,
                                   recall_threshold = recall_threshold,
                                   feature_selection = feature_selection)
    copy(ensemble).test(features, toxicity)
    loo = LeaveOneOut()
    ypred = []
    for train_index, test_index in loo.split(features):
        if regularizer is None:
            xtrain, xtest = Metrics.normalize_and_drop(features[train_index] , features[test_index])
        else:
            xtrain = regularizer.fit_transform(features[train_index])
            xtest = regularizer.transform(features[test_index])
        ensemble.fit(xtrain, toxicity[train_index])
        ypred.append(ensemble.predict_proba(xtest)[0])
    ypred = np.array(ypred)
    ypred /= ypred.max()
    print(roc_auc_score(toxicity, ypred), recall_score(toxicity, ypred > 0))
    print(ClusterStats().get_contingency_table(ypred, toxicity))

def roc_cv(classifier, x, y, feature_selection_method = None, regularizer = None, resampler = None):
    x = feature_selection(x, y, feature_selection_method)
    loo = LeaveOneOut()
    ypred = np.zeros(y.shape)
    regularizer = QuantileTransformer() if regularizer is None else regularizer
    for train_idx, test_idx in loo.split(x):
        xtrain, ytrain = x[train_idx], y[train_idx]
        if resampler is not None:
            xtrain, ytrain = resampler.fit_resample(xtrain, ytrain)
        xtrain = regularizer.fit_transform(xtrain)
        xtest = regularizer.transform(x[test_idx])
        classifier.fit(xtrain, ytrain)
        ypred[test_idx] = classifier.predict_proba(xtest)[0,1]
    results = {}
    results['AUC'] = roc_auc_score(y, ypred)
    results['f1'] = f1_score(y, ypred > .5)
    results['output'] = ypred
    results['resampler'] = resampler
    results['regularizer'] = regularizer
    results['classifier'] = classifier.fit(x,y)
    return results

def presplit_roc_cv(classifier, data_split):
    ypred = np.zeros((len(data_split),))
    y = np.array([split['ytest'] for split in data_split])
    i = 0
    for split in data_split:
        classifier.fit(split['xtrain'], split['ytrain'])
        ypred[i] = classifier.predict_proba(split['xtest'])[0,1]
        if i == 0:
            has_importances = hasattr(classifier, 'feature_importances_')
        if has_importances:
            if i == 0:
                importances = classifier.feature_importances_
            else:
                importances += classifier.feature_importances_
        i += 1
    if has_importances:
        importances /= i
        importances = pd.Series(data = importances, index = data_split[0]['feature_labels'])
    else:
        importances = None
    return roc_auc_score(y, ypred), importances


def organ_data_to_df(arr,
                     ids = None,
                     suffix = '_distance',
                     organ_list = None,
                     to_merge = None):
    organ_list = organ_list if organ_list is not None else Constants.organ_list
    print(arr.shape[1], len(organ_list))
    assert(arr.shape[1] == len(organ_list))
    columns = [o+suffix for o in organ_list]
    if to_merge is not None and ids is None:
        ids = to_merge.index.values
    df = pd.DataFrame(arr, index = ids, columns = columns)
    if to_merge is not None:
        df = df.join(to_merge, how = 'inner')
    return df

def feature_selection(x,y, method):
    return x

def generate_features(db = None,
                      file = 'data/ds_topFeatures.csv',
                      db_features = ['hpv'],
                      db_organ_list = None):
    df = pd.read_csv(file, index_col = 1).drop('Unnamed: 0', axis = 1).sort_index()
    df['T.category'] = df['T.category'].apply(lambda x: int(x[1]))
    ft = df.FT.values
    ar = df.AR.values
    tox = df.TOX.values
    if db is None and db_features is not None and len(db_features) > 0:
        db = PatientSet(root = 'data\\patients_v*\\',
                        use_distances = False)
        df = db.to_dataframe(db_features, df, organ_list = db_organ_list)
    df = df.drop(['FT', 'AR', 'TOX'], axis = 1)
    return  df, ft, ar, tox

def generate_baseline_features(db = None,
                               file = 'data/baselineClustering.csv',
                               db_features = [],
                               db_organ_list = None,
                               df1_features = ['hc_ward'],
                               binarize = True,
                               to_drop = ['manhattan_k=2', 'manhattan_k=3', 'manhattan_k=4']):
    df = pd.read_csv(file, index_col = 'Dummy.ID').drop('Unnamed: 0', axis = 1).sort_index()
    ft = df.FT.values
    ar = df.AR.values
    tox = df.TOX.values
    if db is None and db_features is not None and len(db_features) > 0:
        db = PatientSet(root = 'data\\patients_v*\\',
                        use_distances = False)
        df = db.to_dataframe(db_features, df, organ_list = db_organ_list)
    if df1_features is not None and len(df1_features) > 0:
        df1 = generate_features(db)[0]
        for feature in df1_features:
            df[feature] = df1[feature]
    if binarize:
        above_average = lambda name: df[name].apply(lambda x: x > df[name].values.mean())
        df['Total.dose'] = above_average('Total.dose')
        df['Age.at.Diagnosis..Calculated.'] = above_average('Age.at.Diagnosis..Calculated.')
    if to_drop is not None:
        df = df.drop(to_drop, axis = 1)
    df = df.drop(['FT', 'AR', 'TOX'], axis = 1)
    return df, ft, ar, tox

def generate_cluster_features(db = None,
                              db_features = [],
                              baseline_file = 'data/baselineClustering.csv',
                              top_clustering_file = 'data/ds_topFeatures.csv',
                              db_organ_list = None,
                              use_baseline_features = True,
                              use_top_features = False,
                              discrete_features = False,
                              cluster_names = ['hc_ward4','kmeans_k=4']):
    baseline = pd.read_csv(baseline_file, index_col = 'Dummy.ID').drop('Unnamed: 0', axis = 1)
    dist_clusters = pd.read_csv(top_clustering_file, index_col = 'Dummy.ID').drop('Unnamed: 0', axis = 1)
    all_clusters = set(['manhattan_k=2',
                    'manhattan_k=3',
                    'manhattan_k=4',
                    'hc_ward2',
                    'hc_ward4',
                    'nb_clust',
                    'FT',
                    'AR',
                    'TOX'])
    non_features = list(all_clusters - set(cluster_names))
    if use_baseline_features:
        cluster_names = cluster_names + list(baseline.drop(non_features, axis = 1, errors='ignore').columns)
    if use_top_features:
        cluster_names = cluster_names + list( dist_clusters.drop(non_features,axis=1, errors='ignore').columns)
    if 'T.category' in cluster_names:
        dist_clusters['T.category'] = dist_clusters['T.category'].apply(lambda x: int(x[1]))
    df = baseline.merge(dist_clusters, on=['Dummy.ID','FT','AR','TOX'])
    ft = df.FT.values
    ar = df.AR.values
    tox = df.TOX.values
    to_drop = set(df.columns) - set(cluster_names)
    if db_features is not None and len(db_features) > 0:
        if db is None:
            db = PatientSet(root = 'data\\patients_v*\\',
                            use_distances = False)
        df = db.to_dataframe(db_features, df, organ_list = db_organ_list)
    df = df.drop(to_drop, axis = 1, errors = 'ignore')
    if discrete_features:
        df = discretize_continuous_fields(df, 5)
    columns = df.columns
    for col in columns:
        if col in all_clusters:
            groups = set(df[col].values)
            for g in groups:
                col_name = col + '=' + str(g)
                df[col_name] = df[col].values == g
            df = df.drop(col, axis = 1)
    print(df.columns)
    return df, ft, ar, tox

def discretize_continuous_fields(df, n_bins):
    encoder = KBinsDiscretizer(n_bins = n_bins, encode = 'ordinal')
    for col in df.columns:
        vals = df[col].values
        if len(np.unique(vals)) > n_bins:
            df[col] = encoder.fit_transform(vals.reshape(-1,1)).ravel()
    return df

def test_classifiers(db = None, log = False,
                     feature_params = {},
                     predicted_doses = None,
                     pdose_organ_list = None,
                     regularizer = QuantileTransformer(),
                     additional_features = None,
                     data_splits = None,
                     print_importances = True):

    result_template = {'cluster_names': copy(str(feature_params['cluster_names'] + feature_params['db_features'])),
                       'Baseline': str(feature_params['use_baseline_features']),
                       'Top_features': str(feature_params['use_top_features'])}

    if log:
        timestamp = datetime.fromtimestamp(time()).strftime('%Y_%m_%d_%H%M%S')
        f = open(Constants.toxicity_log_file_root + timestamp +'.txt', 'w', buffering = 1)
        def write(string):
            print(string)
            f.write(str(string)+'\n')
    else:
        write = lambda string: print(string)
    feature_params['db'] = db
    df, ft, ar, tox = generate_cluster_features(**feature_params)

    if predicted_doses is not None:
        if not isinstance(predicted_doses, np.ndarray):
            predicted_doses = default_rt_prediction(db)
        pdose_organ_list = Constants.organ_list if pdose_organ_list is None else pdose_organ_list
        if len(pdose_organ_list) > 0:
            o_args = np.array([Constants.organ_list.index(o) for o in pdose_organ_list if o in Constants.organ_list])
            df = organ_data_to_df(predicted_doses[:,o_args],
                              suffix = '_pdoses',
                              to_merge = df,
                              organ_list = pdose_organ_list)

    write('features: ' + ', '.join([str(c) for c in df.columns]) + '\n')
    outcomes = [(ft, 'feeding_tube'), (ar, 'aspiration')]
    from xgboost import XGBClassifier
    classifiers = [
#                    DecisionTreeClassifier(),
#                    DecisionTreeClassifier(criterion='entropy'),
#                    XGBClassifier(1, booster = 'gblinear'),
#                    XGBClassifier(3, booster = 'gblinear'),
#                    XGBClassifier(5, booster = 'gblinear'),
#                    XGBClassifier(),
#                    XGBClassifier(booster = 'dart'),
                    LogisticRegression(C = 1, solver = 'lbfgs', max_iter = 3000),
#                    MetricLearningClassifier(use_softmax = True),
#                    MetricLearningClassifier(
#                            resampler = under_sampling.OneSidedSelection()),
#                    MetricLearningClassifier(
#                            resampler = under_sampling.CondensedNearestNeighbour()),
#                    ExtraTreesClassifier(n_estimators = 200),
#                    RandomForestClassifier(n_estimators = 200, max_depth = 3),
#                    BayesWrapper(),
                   ]
    data_splits = get_all_splits(df, regularizer, outcomes) if data_splits is None else data_splits
    print('splits finished')
    results = []
    for classifier in classifiers:
        write(classifier)
        for outcome in outcomes:
            data_split = data_splits[outcome[1]]
            for resampler_name, splits in data_split.items():
                try:
                    write(resampler_name)
                    auc, importances = presplit_roc_cv(classifier, splits)
                    write(outcome[1])
                    write(auc)
                    if importances is not None:
                        write(importances)
                    write('\n')
                    result = copy(result_template)
                    result['classifier'] = str(classifier)
                    result['outcome'] = str(outcome[1])
                    result['resampler'] = str(resampler_name)
                    result['AUC'] = auc
                    results.append(result)
                except:
                    continue
    if log:
        f.close()
    return results

def get_all_splits(df, regularizer, outcomes, resamplers = None):
    if resamplers is None:
        resamplers = [None,
                  under_sampling.RandomUnderSampler(),
                  over_sampling.RandomOverSampler(),
#                  under_sampling.InstanceHardnessThreshold(
#                          estimator = MetricLearningClassifier(),
#                          cv = 18),
                  under_sampling.InstanceHardnessThreshold(cv = 18),
                  over_sampling.SMOTE(),
                  combine.SMOTEENN(),
                  combine.SMOTETomek(),
                  under_sampling.InstanceHardnessThreshold(),
                  under_sampling.RepeatedEditedNearestNeighbours(),
                  under_sampling.EditedNearestNeighbours(),
                  under_sampling.CondensedNearestNeighbour(),
                  under_sampling.OneSidedSelection(),
                  ]
    data_splits = {}
    for outcome in outcomes:
        splits = {str(resampler): get_splits(df, outcome[0], regularizer, [resampler]) for resampler in resamplers}
        data_splits[outcome[1]] = splits
    return data_splits

def get_splits(df, y, regularizer = None, resamplers = None):
    x = df.values
    feature_labels = list(df.columns)
    loo = LeaveOneOut()
    splits = []
    for train, test in loo.split(x):
        split = {}
        xtrain, ytrain = x[train], y[train]
        xtest, ytest = x[test], y[test]
        if regularizer is not None:
            xtrain = regularizer.fit_transform(xtrain)
            xtest = regularizer.transform(xtest)
        for resampler in resamplers:
            if resampler is None:
                continue
            xtrain, ytrain = resampler.fit_resample(xtrain, ytrain)
        split['xtrain'] = xtrain
        split['xtest'] = xtest
        split['ytrain'] = ytrain
        split['ytest'] = ytest
        split['train_index'] = train
        split['test_index'] = test
        split['feature_labels'] = feature_labels
        splits.append(split)
    return splits

def plot_correlations(db,
                      use_predicted_dose = True,
                      use_distances = True,
                      use_volumes = True,
                      max_p = .15,
                      tox_name = 'toxicity',
                      pred_doses = None):
    db_features = ['hpv', 'smoking', 'ages',
                   'packs_per_year', 'dose_fractions',
                   'prescribed_doses', 'has_gtvp']
    data, ft, ar, tox = generate_features(db, db_features =db_features)
    if tox_name == 'toxicity':
        y = tox
    elif tox_name in ['feeding_tube', 'ft']:
        y = ft
    elif tox_name in ['aspiration', 'ar', 'aspiration_rate']:
        y = ar
    if use_predicted_dose:
        if pred_doses is None:
            pred_doses = default_rt_prediction(db)
        data = organ_data_to_df(pred_doses,
                                ids = db.ids, to_merge = data,
                                suffix = '_pred_dose')
    if use_distances:
        data = organ_data_to_df(db.tumor_distances,
                                ids = db.ids, to_merge = data,
                                suffix = '_tumor_distance')
    if use_volumes:
        data = organ_data_to_df(db.volumes,
                                ids = db.ids, to_merge = data,
                                suffix = '_volumes')
    clusters = data.hc_ward.values
    high_cluster = np.argwhere(clusters == 2).ravel()
    print(len(high_cluster))
    toxicity = np.argwhere(y > 0).ravel()
    outliers = set(high_cluster) - set(toxicity)
    inliers = set(high_cluster) - outliers
    data = data.assign(classes = pd.Series(-np.ones(y.shape), index = data.index).values)
    data.classes.iloc[sorted(inliers)] = 0
    data.classes.iloc[sorted(outliers)] = 1
    inlier_data = (data[data.classes == 0])
    outlier_data = (data[data.classes == 1])
    print(inlier_data.index)
    print(outlier_data.index)
    pvals = {}
    for col in sorted(data.drop(['hc_ward', 'classes'], axis = 1).columns):
        v1 = inlier_data[col].values
        v2 = outlier_data[col].values
        pval = kruskal(v1, v2).pvalue
        if pval < max_p:
            pvals[col] = pval
    sorted_pvals = sorted(pvals.items(), key = lambda x: x[1])
    print(sorted_pvals)
    labels, vals = list(zip(*sorted_pvals))
    plt.barh(np.arange(len(vals)), max_p-np.array(vals), tick_label = labels, left = 1-max_p)
    plt.xlabel('1 - pvalue for kruskal-wallis test')
    plt.title('1 - pvalue between cluster 2 with and without ' + tox_name + ' per feature')

    discrete_dists = Metrics.discretize(-db.tumor_distances, n_bins = 15, strategy='uniform')
    t_volumes = np.array([np.sum([g.volume for g in gtvs]) for gtvs in db.gtvs]).reshape(-1,1)
    discrete_volumes = Metrics.discretize(t_volumes, n_bins = 15, strategy='uniform')

def augmented_db(db = None, db_args = {}):
    if db is None:
        db = PatientSet(**db_args)
    db.discrete_dists = Metrics.discretize(-db.tumor_distances, n_bins = 15, strategy='uniform')
    db.t_volumes = np.array([np.sum([g.volume for g in gtvs]) for gtvs in db.gtvs]).reshape(-1,1)
    db.bilateral = db.lateralities == 'B'
    db.pdoses = default_rt_prediction(db)
    db.t4 = db.t_categories == 'T4'
    db.m_volumes = db.volumes[:, [Constants.organ_list.index('Rt_Masseter_M'), Constants.organ_list.index('Lt_Masseter_M')]].sum(axis = 1).ravel()
    return(db)


#db = augmented_db()

feature_params = {
                  'db_features': [],
                  'db_organ_list': None,
                  'use_baseline_features': True,
                  'use_top_features': False,
                  'discrete_features': False,
                  'cluster_names': []
                  }
all_results = []
run = lambda x: test_classifiers(db, log = True, feature_params = x)

for cluster_combo in [['hc_ward2'],['hc_ward4'],['manhattan_k=4'],['hc_ward4', 'manhattan_k=4'],['nb_clust'],[]]:
    feature_params['cluster_names'] = cluster_combo
    all_results.extend(run(feature_params))


feature_params['use_baseline_features'] = False
feature_params['cluster_names'] = ['hc_ward4']
all_results.extend(test_classifiers(db, log = True, feature_params = feature_params))

feature_params['cluster_names'] = ['manhattan_k=4']
all_results.extend(test_classifiers(db, log = True, feature_params = feature_params))

feature_params['cluster_names'] = ['hc_ward2','manhattan_k=2']
all_results.extend(test_classifiers(db, log = True, feature_params = feature_params))

feature_params['cluster_names'] = ['hc_ward4','manhattan_k=4']
all_results.extend(test_classifiers(db, log = True, feature_params = feature_params))

feature_params['db_features'] =['discrete_dists', 'bilateral', 'prescribed_doses', 'dose_fractions','m_volumes', 't_categories']
feature_params['db_organ_list'] = ['SPC', 'Tongue']
pdose_organs = ['IPC','Larynx', 'Supraglottic_Larynx', 'SPC', 'Soft_Palate','Genioglossus_M', 'Tongue', 'Extended_Oral_Cavity']
all_results.extend(test_classifiers(db, log = False, feature_params = feature_params, pdose_organ_list = pdose_organs, predicted_doses=db.pdoses))

df = pd.DataFrame(all_results).sort_values(
        ['classifier',
         'outcome',
         'AUC',
         'resampler',
         'cluster_names',
         'Baseline'],
         kind = 'mergesort',
         ascending = False)
df.to_csv('data/toxcity_classification_tests_'
          + datetime.fromtimestamp(time()).strftime('%Y_%m_%d_%H%M%S')
          + '.csv', index = False)

#plot_correlations(db, max_p = .25, pred_doses = p_doses)