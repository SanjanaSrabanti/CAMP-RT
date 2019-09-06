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
from Toxicity import ClusterStats
from copy import copy
import numpy as np
from Boruta import BorutaPy
from sklearn.feature_selection import mutual_info_classif, f_classif, SelectPercentile, SelectKBest
from sklearn.model_selection import cross_validate, cross_val_predict, LeaveOneOut
from sklearn.metrics import accuracy_score, recall_score, roc_auc_score, roc_curve, f1_score
from sklearn.naive_bayes import BernoulliNB, ComplementNB, GaussianNB
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import OneHotEncoder, QuantileTransformer, MinMaxScaler, RobustScaler, StandardScaler
from sklearn.cluster import AgglomerativeClustering
from sklearn.tree import DecisionTreeClassifier
from sklearn.discriminant_analysis import QuadraticDiscriminantAnalysis
from sklearn.ensemble import VotingClassifier, ExtraTreesClassifier, RandomForestClassifier, AdaBoostClassifier
from sklearn.svm import SVC
from NCA import NeighborhoodComponentsAnalysis
from sklearn.base import BaseEstimator
from collections import namedtuple, OrderedDict
from imblearn import under_sampling, over_sampling, combine

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

class MetricLearningClassifier(BaseEstimator):

    def __init__(self, n_components = None, init = 'pca', random_state = 1, resampler = None):
        self.nca = NeighborhoodComponentsAnalysis(n_components = n_components,
                                                  init = init,
                                                  max_iter = 1000,
                                                  random_state = random_state)
        self.group_parameters = namedtuple('group_parameters', ['means', 'inv_covariance', 'max_dist'])
        self.resampler = resampler

    def fit(self, x, y):
        if self.resampler is not None:
            x,y = self.resampler.fit_resample(x,y)
        self.nca.fit(x, y)
        self.groups = OrderedDict()
        for group in np.unique(y):
            self.groups[group] = self.group_params(x, y, group)

    def group_params(self, x, y, group):
        targets = np.argwhere(y == group).ravel()
        x_target = self.nca.transform(x[targets])
        fmeans = x_target.mean(axis = 0)
#        try:
#            inv_cov = np.linalg.inv(np.cov(x_target.T))
#        except:
        inv_cov = np.linalg.pinv(np.cov(x_target.T))
        train_dists = self.mahalanobis_distances(x, self.group_parameters(fmeans, inv_cov, 0))
        parameters = self.group_parameters(fmeans, inv_cov, train_dists.max())
        return parameters

    def mahalanobis_distances(self, x, group):
        x_offset = self.nca.transform(x) - group.means
        left_term = np.dot(x_offset, group.inv_covariance)
        mahalanobis = np.dot(left_term, x_offset.T).diagonal()
        return mahalanobis

    def predict_proba(self, x):
        all_distances = []
        for group_id, group_params in self.groups.items():
            distances = self.mahalanobis_distances(x, group_params)
            proximity = np.clip(1 - (distances/group_params.max_dist), 0, 1)
            all_distances.append(proximity)
        output = np.hstack(all_distances).reshape(-1, len(self.groups.keys()))
        return output#/output.sum(axis = 1).reshape(-1,1)

    def predict(self, x):
        labels = list(self.groups.keys())
        probs = self.predict_proba(self, x)
        return np.argmax(probs, axis = 1).ravel()

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


def feature_selection(x,y, method):
    return x


def test_classifiers(db = None, log = False, extra_df= None):
    if db is None:
        db = PatientSet(root = 'data\\patients_v*\\',
                        use_distances = False)
    if log:
        f = open(Constants.toxicity_log_file, 'w')
        def write(string):
            print(string)
            f.write(str(string)+'\n')
    else:
        write = lambda string: print(string)
    df = pd.read_csv('data/ds_topFeatures.csv', index_col = 1).drop('Unnamed: 0', axis = 1)
    df['T.category'] = df['T.category'].apply(lambda x: int(x[1]))
    ft = df.FT.values
    ar = df.AR.values
    tox = df.TOX.values
    df = db.to_dataframe(['hpv'],df)
    df = df.drop(['FT', 'AR', 'TOX'], axis = 1)
    if extra_df is not None:
        x = extra_df.values
#        x = df.join(extra_df, how = 'inner').values
    else:
        x = df.values
    classifiers = [
                    LogisticRegression(C = 0.1, solver = 'lbfgs', max_iter = 3000),
                    LogisticRegression(C = 1, solver = 'lbfgs', max_iter = 3000),
#                    LogisticRegression(C = 10, solver = 'lbfgs', max_iter = 3000),
#                    LogisticRegression(C = 100, solver = 'lbfgs', max_iter = 3000),
#                    QuadraticDiscriminantAnalysis(),
#                   RandomForestClassifier(n_estimators = 300),
#                   RandomForestClassifier(n_estimators = 300, min_samples_split = 4),
                   MetricLearningClassifier(
                            resampler = under_sampling.OneSidedSelection()),
                    MetricLearningClassifier(
                            resampler = under_sampling.CondensedNearestNeighbour()),
                    MetricLearningClassifier(),
                    KNeighborsClassifier(5),
#                    KNeighborsClassifier(2),
#                   ExtraTreesClassifier(n_estimators = 300),
#                   AdaBoostClassifier(n_estimators = 20, learning_rate = .01),
#                    AdaBoostClassifier(n_estimators = 50, learning_rate = .01),
#                    AdaBoostClassifier(n_estimators = 100, learning_rate = .01),

                   ]
    results = []
    resamplers = [None,
#                  under_sampling.EditedNearestNeighbours(),
#                  under_sampling.CondensedNearestNeighbour(),
#                  under_sampling.TomekLinks(),
#                  under_sampling.NearMiss(),
#                  under_sampling.NeighbourhoodCleaningRule(),
                  under_sampling.InstanceHardnessThreshold(),
#                  combine.SMOTEENN(),
#                  combine.SMOTETomek(),
#                  over_sampling.SMOTE(),
                  ]
    for classifier in classifiers:
        write(classifier)
        for resampler in resamplers:
            write(resampler)
            for outcome in [(ft, 'feeding_tube'), (ar, 'aspiration')]:
                try:
                    roc = roc_cv(classifier, x, outcome[0],
                                 regularizer = QuantileTransformer(),
                                 resampler = resampler)
                    roc['outcome'] = outcome[1]
                    write(outcome[1])
                    write(roc['AUC'])
                    results.append(roc)
                except:
                    write('\n')
                    write('Error with ' + str(classifier) + ' ' + str(resampler))
                    write('\n\n')
            write('\n')
    if log:
        f.close()

#test_classifiers(db)
#db = PatientSet(root = 'data\\patients_v*\\',
#                        use_distances = False)
df = pd.read_csv('data/selected_features.csv', index_col = 0)
test_classifiers(db, extra_df= df)