#functions for getting clusters
import numpy as np
import pandas as pd

from analysis import *
from collections import namedtuple
import Metrics
from PatientSet import PatientSet

#for getting the fisher exact test
import rpy2.robjects.numpy2ri
from rpy2.robjects.packages import importr
rpy2.robjects.numpy2ri.activate()


from scipy.cluster.hierarchy import fcluster, linkage
from sklearn.metrics import adjusted_rand_score, f1_score, roc_auc_score
from sklearn.cluster import AffinityPropagation, AgglomerativeClustering, KMeans
from sklearn.preprocessing import StandardScaler
from sklearn.utils import resample

def l1(x1, x2):
    return np.sum(np.abs(x1-x2))

def tanimoto_dist(x1, x2):
    if l1(x1 - x2) == 0:
        return 0
    tanimoto = x1.dot(x2)/(x1.dot(x1) + x2.dot(x2) - x1.dot(x2))
    return 1/(1+tanimoto)

def l2(x1, x2):
    return np.sqrt(np.sum((x1-x2)**2))

def pdist(x, dist_func):
    distance = []
    for i in range(x.shape[0]):
        for j in range(x.shape[0]):
            distance.append(dist_func(x[i], x[j]))
    return np.array(distance)

class FClusterer():

    def __init__(self, n_clusters, dist_func = l1, link = 'weighted', criterion = 'maxclust'):
        self.link = link
        self.dist_func = dist_func if link not in ['median', 'ward', 'centroid'] else 'euclidean'
        self.t = n_clusters
        self.criterion = criterion

    def fit_predict(self, x, y = None):
        clusters = linkage(x, method = self.link, metric = self.dist_func)
        return fcluster(clusters, self.t, criterion = self.criterion)

class FeatureClusterer():
    #clusters features together
    #like sklearn feature agglomeration, but can work on dataframes and tracks names of the features

    def __init__(self, base_model = 'default', scale = False):
        if base_model is None or base_model == 'default':
            self.base_model = AffinityPropagation()
        else:
            self.base_model = base_model
        self.scale = scale
        assert(hasattr(self.base_model, 'fit_predict'))

    def fit(self, x, y = None):
        if self.scale:
            x = StandardScaler().fit_transform(x)
        x = x.transpose()
        self.labels = self.base_model.fit_predict(x)
        self.labels = self.map_to_zero(self.labels)

    def map_to_zero(self, labels):
        labels -= labels.min()
        unique_labels = set(labels)
        n_labels = len(unique_labels)
        if n_labels == labels.max():
            return labels
        for i in range(n_labels):
            if i not in set(labels):
                args = np.argwhere(labels > i)
                labels[args] -= 1
        return labels

    def predict(self, x, y = None):
        index = list(x.index)
        x = x.transpose()
#        if hasattr(self.base_model, 'predict'):
#            labels = self.base_model.predict(x)
#        else:
#            labels = self.base_model.fit_predict(x)
#        labels = self.map_to_zero(labels)
#        print(sorted(set(labels)))
        is_df = isinstance(x, pd.DataFrame)

        groups = [[] for x in range(len(set(self.labels)))]
        group_names = [[] for x in range(len(set(self.labels)))]
        for pos, groupnum in enumerate(self.labels):
            if is_df:
                feature = x.iloc[pos]
                groups[groupnum].append(feature.values)
                group_names[groupnum].append(feature.name)
            else:
                groups[groupnum].append(x[pos])

        f_out = np.zeros((len(set(self.labels)), x.shape[1]))
        for row, vals in enumerate(groups):
            f_out[row] = np.mean(vals, axis = 0)
        x_out = f_out.transpose()
        group_names = [','.join(gn) for gn in group_names]
        if is_df:
            x_out = pd.DataFrame(x_out, index=index, columns = group_names)
        return x_out

    def fit_predict(self, x, y = None):
        self.fit(x)
        return self.predict(x)

class FeatureSelector():

    def __init__(self, model = None, n_samples = 1, rescale = True, threshold = .0001):
        if model is None:
            from sklearn.linear_model import LogisticRegression
            model = LogisticRegression(C = 100, solver = 'lbfgs', max_iter = 10000)
        self.model = model
        self.n_samples = n_samples
        self.threshold = threshold
        self.rescale = rescale

    def get_importances(self, x, y, x_val = None):
        importances = np.zeros((x.shape[1],))
        for pos, col in enumerate(x.columns):
            if x_val is not None:
                xtrain = x_val.join(x[col], how='inner').values
            else:
                xtrain = x[col].values
            score = self.bootstrap_score(xtrain, y)
            importances[pos] += score
        return importances

    def bootstrap_score(self, x, y, metric = roc_auc_score):
        x = x.astype('int32')
        score = 0
        for dummy in range(self.n_samples):
            if self.n_samples > 1:
                xtemp, ytemp = resample(x, y)
            else:
                xtemp, ytemp = x, y
            if xtemp.ndim == 1:
                xtemp = xtemp.reshape(-1,1)
            ypred = self.cv_predict(xtemp, ytemp)
            score += metric(ytemp.ravel(), ypred.ravel())/self.n_samples
        return score

    def cv_predict(self, x, y):
        ypred = np.zeros(y.shape)
        for d in range(y.shape[0]):
            xtrain = np.delete(x, d, axis = 0)
            ytrain = np.delete(y, d, axis = 0)
            xtest = x[d].reshape(1, -1)
            if self.rescale:
                xtrain, xtest = Metrics.rescale(xtrain, xtest)
            self.model.fit(xtrain, ytrain)
            ypred[d] = self.model.predict_proba(xtest)[:,1]
        return ypred

    def get_most_important(self, x, y, x_val = None):
        importances = self.get_importances(x,y, x_val)
        fname = x.columns[np.argmax(importances)]
        return fname, importances.max()

    def fit(self, x, y):
        x = x.copy()
        top_feature, best_score = self.get_most_important(x, y)
        features_to_keep = [top_feature]
        while len(features_to_keep) < x.shape[1]:
            x_remaining = x.drop(features_to_keep, axis = 1)
            next_best_feature, _ = self.get_most_important(x_remaining,y,
                                                           x_val = x.loc[:, features_to_keep])
            new_featureset = features_to_keep + [next_best_feature]
            xtemp = x.loc[:, new_featureset].values
            new_score = self.bootstrap_score(xtemp, y)
#            self.model.fit(xtemp, y.reshape(-1,1))
#            ypred = self.model.predict_proba(xtemp)[:,1]
#            new_score = roc_auc_score(y.ravel(), ypred.ravel())
            if new_score < best_score + self.threshold:
                break
            best_score = new_score
            features_to_keep = new_featureset
            print(features_to_keep)
            print(best_score)
            print()
        self.features_to_keep = features_to_keep
        print(features_to_keep)
        self.feature_inds = [np.argwhere(x.columns == f)[0][0] for f in features_to_keep]
        return self

    def transform(self, x, y = None):
        return x.iloc[:, self.feature_inds]

    def fit_transform(self, x, y):
        self.fit(x,y)
        return self.transform(x,y)

class FeatureClusterSelector(FeatureSelector):

    def __init__(self, clusterer = None, n_samples = 1, rescale = True, threshold = 0):
        if clusterer is None:
            from sklearn.cluster import AgglomerativeClustering
            self.clusterer = AgglomerativeClustering(n_clusters = 4)
        else:
            self.clusterer = clusterer
        self.n_samples = 1
        self.rescale = rescale
        self.threshold = threshold

    def bootstrap_score(self, x, y):
        x = x.astype('int32')
        score = 0
        for dummy in range(self.n_samples):
            if self.n_samples > 1:
                xtemp, ytemp = resample(x, y)
            else:
                xtemp, ytemp = x, y
            if xtemp.ndim == 1:
                xtemp = xtemp.reshape(-1,1)
            clusters = self.clusterer.fit_predict(xtemp).ravel()
            score += (1-fisher_exact_test(clusters, ytemp))/self.n_samples
        return score



cluster_result = namedtuple('cluster_result', ['method', 'cluster', 'correlation','rand_score', 'model'])

def get_sortable_metric(c_result, metric):
    #function so we can use both metrics as parameters to pick clustering with
    #we want a small correlation, but large rand score
    assert(metric in ['correlation', 'rand_score'])
    if metric == 'correlation':
        return c_result.correlation
    else:
        return -c_result.rand_score

def get_clusterers(min_clusters = 4, max_clusters = 4):
    c_range = range(min_clusters, max_clusters + 1)
    clusterers = {}
#    clusterers['l1_weighted'] = [FClusterer(c) for c in c_range]
#    clusterers['l2_weighted'] = [FClusterer(c, dist_func = l2) for c in c_range]
#    clusterers['centroid'] = [FClusterer(c, link='centroid') for c in c_range]
#    clusterers['median'] = [FClusterer(c, link = 'median') for c in c_range]
    clusterers['ward'] = [AgglomerativeClustering(c) for c in c_range]
#    clusterers['Kmeans'] = [KMeans(c) for c in c_range]
    return clusterers

def fisher_exact_test(c_labels, y):
    if len(set(y)) == 1:
        print('fisher test run with no positive class')
        return 0
#        assert(len(set(y)) == 2)
    #call fishers test from r
    contingency = get_contingency_table(c_labels, y)
    stats = importr('stats')
    pval = stats.fisher_test(contingency)[0][0]
    return pval

def get_contingency_table(x, y):
    #assumes x and y are two equal length vectors, creates a mxn contigency table from them
    cols = sorted(list(np.unique(y)))
    rows = sorted(list(np.unique(x)))
    tabel = np.zeros((len(rows), len(cols)))
    for row_index in range(len(rows)):
        row_var = rows[row_index]
        for col_index in range(len(cols)):
            rowset = set(np.argwhere(x == row_var).ravel())
            colset = set(np.argwhere(y == cols[col_index]).ravel())
            tabel[row_index, col_index] = len(rowset & colset)
    return tabel

def analyze_clusters(target_var, name, clusterer, features, metric = 'correlation'):
    clusters = clusterer.fit_predict(features).ravel()
    n_clusters = len(set(clusters))
    if n_clusters < 2:
        return None
    method = name + str(n_clusters)

    overall_correlation = fisher_exact_test(clusters, target_var)
    rand_score = adjusted_rand_score(clusters, target_var)
    result = cluster_result(method,
                            'all',
                            overall_correlation,
                            rand_score,
                            clusterer)
    return result

def cluster(target_var, features,
            metric = 'correlation',
            args = None,
            min_clusters = 2,
            max_clusters = 4):
    if args is not None:
        assert( isinstance(args, list) )
        features = features[:, args]
    results = []
    clusterers = get_clusterers(min_clusters, max_clusters)
    for cname, clusterers in clusterers.items():
        for clusterer in clusterers:
            analysis = analyze_clusters(target_var, cname, clusterer, features, metric)
            if analysis is not None:
                results.append(analysis)
    results = sorted(results, key = lambda x: get_sortable_metric(x,metric))
    return results

def get_optimal_clustering(features, target_var,
                           metric = 'correlation',
                           args = None,
                           patient_subset = None,
                           min_clusters = 2,
                           max_clusters = 4):
    clusters = np.zeros(target_var.shape)
    if patient_subset is not None:
        target = target_var[patient_subset]
        features = features[patient_subset,:]
    else:
        target = target_var
    result = cluster(target, features,  metric, args)
    if args is not None:
        features = features[:, args]
    clusters[patient_subset] = result[0].model.fit_predict(features).ravel() + 1
    pval = fisher_exact_test(clusters, target_var)
    rand_score = adjusted_rand_score(clusters, target_var)
    clusterer_data = cluster_result(method = result[0].method,
                                    cluster = result[0].cluster,
                                    correlation = pval,
                                    rand_score = rand_score,
                                    model = result[0].model)
    optimal = (clusters, clusterer_data)
    return optimal