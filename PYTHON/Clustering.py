#functions for getting clusters
import numpy as np
import pandas as pd
import re
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
from sklearn.base import ClusterMixin, BaseEstimator

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

class FClusterer(ClusterMixin, BaseEstimator):

    def __init__(self, n_clusters, dist_func = l1, link = 'weighted', criterion = 'maxclust'):
        self.link = link
        self.dist_func = dist_func if link not in ['median', 'ward', 'centroid'] else 'euclidean'
        self.t = n_clusters
        self.criterion = criterion

    def fit_predict(self, x, y = None):
        clusters = linkage(x, method = self.link, metric = self.dist_func)
        return fcluster(clusters, self.t, criterion = self.criterion)

class FeatureClusterer(ClusterMixin, BaseEstimator):
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

class FeatureSelector(BaseEstimator):

    def __init__(self, model = None, n_samples = 1, rescale = True, threshold = .0001,print_out = True):
        if model is None:
            from sklearn.linear_model import LogisticRegression
            model = LogisticRegression(C = 100, solver = 'lbfgs', max_iter = 10000)
        self.model = model
        self.n_samples = n_samples
        self.threshold = threshold
        self.rescale = rescale
        self.print_out = print_out
        self.isfit = False

    def get_importances(self, x, y, baseline = None, as_df = True):
        base_set = set(baseline) if baseline is not None else set([])
        importances = np.zeros((self.n_samples, x.shape[1]))
        for pos, col in enumerate(x.columns):
            if col in base_set:
                continue
            if baseline is not None:
                columns = baseline + [col]
            else:
                columns = [col]
            xtrain = x.loc[:, columns]
            importances[:,pos] = self.bootstrap_score(xtrain, y)
        if as_df:
            return pd.DataFrame(importances, columns = x.columns)
        return importances

    def bootstrap_score(self, x, y, metric = roc_auc_score):
        if isinstance(x, pd.DataFrame) or isinstance(x, pd.Series):
            x = x.copy().values
        x = x.astype('float64')
        score = []
        for dummy in range(self.n_samples):
            if self.n_samples > 1:
                xtemp, ytemp = resample(x, y)
            else:
                xtemp, ytemp = x, y
            if xtemp.ndim == 1:
                xtemp = xtemp.reshape(-1,1)
            ypred = self.cv_predict(xtemp, ytemp)
            score.append(metric(ytemp.ravel(), ypred.ravel()))
        return np.array(score)

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

    def get_most_important(self, x, y, baseline = None):
        importances = self.get_importances(x,y,baseline)
        importances = importances.mean(axis = 0)
        importances = importances.values.ravel()
        fname = x.columns[np.argmax(importances)]
        return fname, importances.max()

    def fit(self, x, y):
        x = x.copy()
        top_feature, best_score = self.get_most_important(x, y)
        features_to_keep = [top_feature]
        while len(features_to_keep) < x.shape[1]:
            next_best_feature, new_score = self.get_most_important(x,y,baseline = features_to_keep)
            if new_score < best_score + self.threshold:
                break
            best_score = new_score
            features_to_keep = features_to_keep + [next_best_feature]
            self.print_updates(features_to_keep, best_score)
     
        self.features_to_keep = features_to_keep
        self.feature_inds = [np.argwhere(x.columns == f)[0][0] for f in features_to_keep]
        self.isfit = True
        return self
    
    def print_updates(self, features_to_keep, best_score):
        if self.print_out == False:
            return
        print(features_to_keep)
        print(best_score)
        print()

    def transform(self, x, y = None):
        assert(self.isfit)
        return x.iloc[:, self.feature_inds]

    def fit_transform(self, x, y):
        self.fit(x,y)
        return self.transform(x,y)

class FeatureClusterSelector(FeatureSelector):

    def __init__(self, clusterer = None, n_samples = 1, rescale = False, threshold = 0, print_out = True):
        if clusterer is None:
            from sklearn.cluster import AgglomerativeClustering
            self.model = AgglomerativeClustering(n_clusters = 4)
        else:
            self.model = clusterer
        self.n_samples = n_samples
        self.rescale = rescale
        self.print_out = print_out
        self.threshold = threshold

    def bootstrap_score(self, x, y):
        if isinstance(x, pd.DataFrame) or isinstance(x, pd.Series):
            x = x.copy().values
        x = x.astype('float64')
        score = []
        for d in range(self.n_samples):
            if self.n_samples > 1:
                xtemp, ytemp = resample(x, y)
            else:
                xtemp, ytemp = x, y
            if xtemp.ndim == 1:
                xtemp = xtemp.reshape(-1,1)
            clusters = self.model.fit_predict(xtemp).ravel()
            score.append(1-fisher_exact_test(clusters, ytemp))
        return np.array(score)
    
    def predict_labels(self, x, y=None):
        assert(self.isfit)
        x = self.transform(x)
        return self.model.fit_predict(x.values, y)
        
    def fit_predict_labels(self, x):
        x = self.fit_transform(x,y)
        return self.model.fit_predict(x.values, y)
    
    def print_updates(self, features_to_keep, best_score):
        if self.print_out == False:
            return
        print(features_to_keep)
        print(1-best_score)
        print()


cluster_result = namedtuple('cluster_result', ['method', 'cluster', 'correlation','rand_score', 'model'])

def get_sortable_metric(c_result, metric):
    #function so we can use both metrics as parameters to pick clustering with
    #we want a small correlation, but large rand score
    assert(metric in ['correlation', 'rand_score'])
    if metric == 'correlation':
        return c_result.correlation
    else:
        return -c_result.rand_score

def get_clusterers(min_clusters = 2, max_clusters = 4):
    c_range = range(min_clusters, max_clusters + 1)
    clusterers = {}
    clusterers['l1_weighted'] = [FClusterer(c) for c in c_range]
#     clusterers['l2_weighted'] = [FClusterer(c, dist_func = l2) for c in c_range]
#     clusterers['centroid'] = [FClusterer(c, link='centroid') for c in c_range]
#     clusterers['median'] = [FClusterer(c, link = 'median') for c in c_range]
#     clusterers['Kmeans'] = [KMeans(c) for c in c_range]
#     clusterers['ward'] = [AgglomerativeClustering(c) for c in c_range]
    return clusterers

def fisher_exact_test(c_labels, y):
    if len(set(y)) == 1:
        print('fisher test run with no positive class')
        return 0
#        assert(len(set(y)) == 2)
    #call fishers test from r
    contingency = get_contingency_table(c_labels, y)
    stats = importr('stats')
    pval = stats.fisher_test(contingency,workspace=2e8)[0][0]
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

def get_train_test_datasets(db, 
                            unclusterables =[], 
                            clusterables =[], 
                            train_only = [], 
                            test_only = [],
                            organs = None,
                            feature_clusterer = None
                           ):
    if organs is None:
        #default organ list is organs - eyeballs since they bad
        organs = copy(Constants.organ_list)
        for o in Constants.organ_list:
            if re.search('Eyeball', o) is not None:
                organs.remove(o)
                
    base = db.to_dataframe(unclusterables,
                           merge_mirrored_organs = True, 
                           organ_list = organs)
    #if we pass a feature_clusterer, performs clustering on the groups of features individually
    for f in clusterables:
        temp_data = db.to_dataframe([f], 
                               merge_mirrored_organs = True, 
                               organ_list = organs)
        if feature_clusterer is not None:
            temp_data = FeatureClusterer(feature_clusterer).fit_predict(temp_data)
        base = base.join(temp_data, how = 'inner')
        
    #baseically train only should be dose and test only should be predicted dose if we want to do that
    def add_featureset(fset, fit, fc):
        if fset is None or len(fset) < 1:
            return base.copy()
        df = db.to_dataframe(fset,
                             merge_mirrored_organs = True,
                             organ_list = organs)
        if fc is not None:
            df = fc.fit_predict(df) if fit else fc.predict(df)
        df = df.join(base, how = 'inner')
        return df.astype('float64')
    
    fc = None
    if feature_clusterer is not None:
        fc = FeatureClusterer(feature_clusterer)
    train = add_featureset(train_only, True, fc)
    test = add_featureset(test_only, False, fc)
    return train.copy(), test.copy()