# -*- coding: utf-8 -*-
"""
Created on Mon Jan 21 17:22:31 2019
@author: Andrew
"""
from glob import glob
from re import findall
import json
import numpy as np
import pandas as pd
from collections import OrderedDict
from skimage.measure import compare_ssim, compare_mse
import random
import matplotlib.pyplot as plt
import pickle
from Constants import Constants
from Patient import Patient

class Rankings():
    #ranking functions that generate a score, takes in pateint objects
    def pca(points):
        points -= np.mean(points, axis = 0)
        cov = np.cov(points, rowvar = False)
        ev, eig = np.linalg.eig(cov)
        principle_components = eig[:,np.argmax(ev)].dot(points.T)
        return(principle_components)
    
    def cluster_organs(db):
        #clusters, and then sorts the clusters and containing values by position along the
        #principle component of the organs
        from sklearn.cluster import AffinityPropagation
        avg = db.get_average_patient_data()
        centroids = avg['centroids']
        estimator = AffinityPropagation()
        estimator.fit(centroids)
        centroid_principle_component = Rankings.pca(centroids)
        #initialize list of empty stuff, and average pca value
        clusters = { label: [[],0] for label in np.unique(estimator.labels_)}
        for x in range(0, Constants.num_organs):
            cluster = clusters[estimator.labels_[x]]
            pc = centroid_principle_component[x]
            cluster[0].append( (Constants.organ_list[x], pc) )
            cluster[0] = sorted(cluster[0], key = lambda x: x[1])
            cluster[1] = cluster[1] + pc
            clusters[estimator.labels_[x]] = cluster
        #average mean pca points
        for key,value in clusters.items():
            clusters[key][1] /= len(clusters[key][0])
        cluster_list = sorted(clusters.values(), key = lambda x: x[1])
        organs = []
        for group in cluster_list:
            for organ_tuple in group[0]:
                organs.append( organ_tuple[0] )
        return(organs)

    def min_dose_error(p1, p2):
        error = np.mean(np.abs(p1.doses - p2.doses))
        return(1/(error + .000001))

    def tumor_distance_matrix(p1):
        x = p1.tumor_distances
        x = x.reshape(len(x),1)*np.absolute(x.reshape(1,len(x)))
        return(x)

    def raw_tumor_organ_ssim(p1, p2):
        return compare_ssim( Rankings.tumor_distance_matrix(p1),
              Rankings.tumor_distance_matrix(p2), win_size = 7)

    def tumor_organ_ssim(p1, p2, weights):
        #this is basically just an ensemble of different distances metrics at this point
        scores = np.zeros((len(weights),))
        if p1.full_dose != p2.full_dose or p1.high_throat_dose != p2.high_throat_dose: #only compare people with full or half radiation with each other
            return 0
        if p1.full_dose == 0 and p1.laterality != p2.laterality: #for half radiation, group by laterality
            return 0
        percent_different = lambda x,y: 1- np.abs(x - y)/(x + y + .0000001)
        if(weights[0] > 0):
            #ssim seems to do better than other things?
            scores[0] = percent_different( p1.tumor_volume/np.sum(p1.volumes), p2.tumor_volume/np.sum(p2.volumes))
            #this one is the most important
        scores[1] = Rankings.raw_tumor_organ_ssim(p1,p2)
        #I was normalizing here, but moved it so I can rescale the data
        final_score = np.sum(scores*weights)/np.mean(weights)
        return(final_score)

class PatientSet():

    def __init__(self, patient_set = None, outliers = [], root = 'data\\patients_v2*\\', score_lower_bound = None, score_lowerer_bound = .4):
        self.score_lower_bound = score_lower_bound
        self.score_lowerer_bound = score_lowerer_bound
        self.good_rankings = []
        self.bad_rankings = []
        if patient_set is None:
            if score_lower_bound is None:
                self.score_lower_bound = .6
            outliers = outliers
            self.total_dose_predictor = None
            (self.patients, self.doses, self.total_doses, self.num_patients, self.ids) = self.read_patient_data(root, outliers)
        else:
            if score_lower_bound is None:
                self.score_lower_bound = patient_set.score_lower_bound
            self.total_dose_predictor = patient_set.total_dose_predictor
            self.patients = patient_set.patients
            self.doses = patient_set.doses
            self.total_doses = patient_set.total_doses
            self.num_patients = patient_set.num_patients
            self.ids = patient_set.ids
        print('\npatient data loaded...\n')

    def read_patient_data(self, root, outliers):
        #sorts by size of largest integer string, which is the id for our files
        file_sort = lambda x: sorted(x, key =
                                     lambda file:
                                         max([int(x) for x in findall("[0-9]+", file)])
                                )
        distance_files = file_sort(glob(root + '**/*distances.csv'))
        dose_files = file_sort(glob(root + '**/*centroid*.csv'))
        #maps ids to position so I can do that too?
        id_map = {max([int(x) for x in findall('[0-9]+', file)]): distance_files.index(file)  for file in distance_files}
        ids = sorted(list(id_map.keys()))
        #delete patient files with an id in the outliers
        for outlier_id in sorted(outliers, reverse = True):
            if outlier_id in id_map:
                pos = id_map[outlier_id]
                del distance_files[pos]
                del dose_files[pos]
                del ids[pos]
        metadata_file = 'data\\patient_info.csv'
        assert(len(distance_files) == len(dose_files))
        #maps a position 0-len(files) to the dummy id for a patient
        num_patients = len(ids)
        metadata = pd.read_csv(metadata_file,
                               index_col = 0, #index is the "Dummy ID"
                               usecols = [0,1,2,3,4,5,6,7,8,9,10,11,31]
                               ).loc[ids]
        patients = OrderedDict()
        dose_matrix = np.zeros((num_patients, len(Constants.organ_list)))
        total_dose_vector = np.zeros((num_patients,))
        #putting all the data into a patient object for further objectification
        for patient_index in range(0, num_patients):
            dataset_version = int(findall('patients_v([0-9])', distance_files[patient_index])[0])
            assert(dataset_version in [2,3])
            #these are indexed by name of organ
            #we only use 3 rows but half of them have a comma missing in the header between the last two rows
            distances = pd.read_csv(distance_files[patient_index],
                                    usecols = [0,1,2]).dropna()
            #renames anything that is equivalent to GTVp/GTVn to the correct format
            distances = self.fix_tumor_names(distances)
            doses = pd.read_csv(dose_files[patient_index],
                                usecols = [0,1,2,3,4,5,6,7]).dropna()
            #pateints_v3 dataset has a different way of ording the columns (and different spelling)
            if dataset_version == 2:
                doses.columns = Constants.centroid_file_names_v2
            elif dataset_version == 3:
                doses.columns = Constants.centroid_file_names_v3
            doses = self.fix_tumor_names(doses)
            #misc patient info - laterality, subsite, total dose, etc
            info = metadata.loc[ids[patient_index]]
            new_patient = Patient(distances, doses,
                                  ids[patient_index], patient_index, info)
            patients[patient_index] = new_patient
            dose_matrix[patient_index, :] = new_patient.doses
            total_dose_vector[patient_index] = new_patient.total_dose
        return((patients, dose_matrix, total_dose_vector, num_patients, ids))
    
    def fix_tumor_names(self, dataframe):
        #this should probably not need to return anything, but does.
        #replace the aliases for GTVp or GTVn(1) with a consistent name
        dataframe.replace(Constants.tumor_aliases, inplace = True)
        dataframe.replace(to_replace = r'GTV.*N', value = 'GTVn', regex = True, inplace = True)
        return dataframe
    
    def export(self, weights = np.array([0,1]) , 
               rank_function = 'tumor_organ_ssim', 
               num_matches = 9,
               patient_data_file = 'data\\patient_dataset.json',
               score_file = 'data\\all_ssim_scores.csv'):
        #exports the dataset into the json format peter is using for the frontend
        data = []
        scores = self.gen_score_matrix(weights, rank_function)
        dose_estimates = self.predict_doses(rank_function, weights, num_matches)
        for p_idx in range(0, self.num_patients):
            patient = self.patients[p_idx]
            entry = patient.to_ordered_dict(dose_estimates[p_idx, :])
            ssim_scores = scores[p_idx,:]
            ssim_scores[p_idx] = 1
            zipped_scores = sorted(zip(ssim_scores, np.arange(1,self.num_patients + 1)), 
                                   key = lambda x: -x[0])
            patient_scores, internal_ids = zip(*zipped_scores)
            entry['similarity_ssim'] = internal_ids
            entry['scores_ssim'] = patient_scores
            data.append(entry)
        #save the vast dictionary of data for the front-end
        try:
            def default(o):
                if isinstance(o, np.int32): 
                    return int(o)  
            with open(patient_data_file, 'w+') as f:  # generate JSON
                json.dump( data, f, indent=4, default = default)
            print('successfully save patient data to ', patient_data_file)
            #save a labeled matrix of similarity scores for other people
        except:
            print('error exporting patient data to json')
        try:
            raw_scores = self.gen_score_matrix(None, 'raw_tumor_organ_ssim')
            score_df = pd.DataFrame(raw_scores, index = self.ids, columns = self.ids)
            score_df.to_csv(score_file)
            print('successfully saved similarity score matrix to ', score_file)
        except:
            print('error saving ssim score matrix')

    def get_patients(self):
        return list(self.patients.values())

    def get_score_dataframe(self, weights, rank_function):
        scores = self.gen_score_matrix(weights, rank_function)
        score_df = pd.DataFrame(scores, index = self.ids, columns = self.ids)
        return(score_df)
    
    def gen_score_matrix(self, weights, rank_function, normalize = False):
        #generates a score matrix based on a rank function
        #function should rank more similar people with a higher number
        
        #normalize == True for if we want to return a vector of scores and rescale them from 0-1 across
        #the whole dataset.  if true compare_traits should be made to return a vector
        if normalize:
            scores = np.zeros((self.num_patients, self.num_patients, len(weights)))
        else:
            scores = np.zeros((self.num_patients, self.num_patients))
        for row in range(0, self.num_patients):
            for col in range(row + 1, self.num_patients):
                scores[row, col] = self.compare_traits(self.patients[row], self.patients[col],
                      rank_function = rank_function, weights = weights)
        if normalize:
            rescale = lambda x: (x - np.min(x))/(np.max(x) - np.min(x) + .000001)
            for score_idx in range(0, len(weights)):
                scores[:, :, score_idx] = rescale(scores[:,:, score_idx])
            scores *= weights
            scores = np.sum(scores, axis = 2)/np.mean(weights)
        #formats it as a symetric matrix with a zero diagonal
        scores += scores.transpose()
        #basically normalize the score so the max is 1?
        scores *= np.absolute(scores)
        scores = scores/scores.max()
        return(scores)

    def predict_doses(self, rank_function, weights, num_matches = 5,
                      td_rank_function = None, td_weights = None, ranks = None):
        #generates an ndarray of dose estimates based on algorithm parameters
        #rank_function and weights are the functions used to match dose
        self.good_rankings = []
        self.bad_rankings = []
        estimates = np.zeros(self.doses.shape)
        if ranks is None:
            ranks = self.gen_score_matrix(rank_function = rank_function, weights = weights)
        for patient_idx in range(0, self.num_patients):
            rank_row = ranks[patient_idx, :]
            estimates[patient_idx, :] = self.estimate_patient_doses(rank_row, num_matches)
            if np.sum(estimates[patient_idx, :]) != 0:
                self.good_rankings.append(patient_idx)
            else:
                self.bad_rankings.append(patient_idx)
        #if a seprate prediction is set for total dose, use that
        if self.total_dose_predictor is not None:
            #normalize the estimates by patient
            estimates /= np.sum(estimates, axis = 1).reshape((self.num_patients,1))
            x = self.gen_patient_feature_matrix()
            total_dose_prediction = self.total_dose_predictor.predict(x)
            estimates *= total_dose_prediction.reshape((self.num_patients,1))
        elif td_rank_function is not None and td_weights is not None:
            estimates /= np.sum(estimates, axis = 1).reshape((self.num_patients,1))
            total_dose_estimates = np.zeros((self.num_patients,))
            td_ranks = self.gen_score_matrix(rank_function = td_rank_function, weights = td_weights)
            for p_idx in range(0,self.num_patients):
                td_rank_row = td_ranks[p_idx, :]
                estimates_for_total_dose = self.estimate_patient_doses(td_rank_row, num_matches)
                total_dose_estimates[p_idx] = np.sum(estimates_for_total_dose)
            estimates *= total_dose_estimates.reshape((self.num_patients,1))
        return(estimates)

    def estimate_patient_doses(self, ranks, num_matches):
            sorted_matches = np.argsort(-ranks)
            good_matches = np.where(ranks > self.score_lower_bound)[0]
            #do some storage of outliers here?
            if len(good_matches) < num_matches and len(good_matches) > 0:
                num_matches = len(good_matches)
            if len(np.where(ranks > min([self.score_lowerer_bound, self.score_lower_bound]))[0]) == 0:
                return np.zeros(self.doses[0,:].shape)
            top_matches = sorted_matches[0:num_matches]
            scores = ranks[top_matches]
            matched_dosages = self.doses[tuple(top_matches), :]
            #weight things by their scores
            for match_idx in range(0, num_matches):
                matched_dosages[match_idx, :] = scores[match_idx]*matched_dosages[match_idx, :]
            patient_estimates = np.mean(matched_dosages, axis = 0)/np.mean(scores)
            return(patient_estimates)
        
    def compare_traits(self, p1, p2, weights, rank_function):
        #calculates an overall scores
        #currently: comparison function, if laterality is equal, difference in tumor volume, tumor distances
        if rank_function == 'tumor_organ_ssim':
            score = Rankings.tumor_organ_ssim(p1,p2,weights = weights)
        elif rank_function == 'raw_tumor_organ_ssim':
            score = Rankings.raw_tumor_organ_ssim(p1,p2)
        elif rank_function == 'min_dose_error':
            score = Rankings.min_dose_error(p1,p2)
        elif rank_function == 'random':
            score = random.random()
        else:
            print('error, invalid rank method: ', rank_function)
        return(score)

    def run_study(self, max_matches = 30, rank_function = 'tumor_organ_ssim', weights = np.array([0,1]),
                  td_rank_function = None, td_weights = None):
        #tests out a metric for a range of difference potential matches and gives a minimum
        ranks = self.gen_score_matrix(rank_function = rank_function, weights = weights)
        base_score = self.predict_doses(rank_function, weights, 1, td_rank_function,
                                        td_weights, ranks = ranks)
        error_hist = []
        error_matrix = np.zeros(( len(self.good_rankings), max_matches - 1))
        error_matrix[:, 0] = np.mean( np.abs( self.doses[self.good_rankings, :] - base_score[self.good_rankings, :]), axis = 1)
        for num_matches in range(2, max_matches):
            estimates = self.predict_doses(rank_function, weights, num_matches,
                                           td_rank_function, td_weights, ranks = ranks)
            error_matrix[:, num_matches - 2] = np.mean( np.abs( self.doses[self.good_rankings, :] - estimates[self.good_rankings, :]), axis = 1)
            error_hist.append(np.mean(error_matrix[:, num_matches - 2]))
        print(rank_function, ': error of', min(error_hist), ' at ', np.argmin(error_hist) + 2)
        return(error_hist, error_matrix)

    def get_average_patient_data(self, key = 'all'):
        #generates a dictionary with *some* (positions, distances, and volumes) of the
        #average data accross all patients
        avg_centroids = np.zeros((45,3))
        avg_volumes = np.zeros((45,))
        avg_distances = np.zeros((45,45))
        avg_tumor_distances = np.zeros((45,))
        avg_tumor_volume = 0.0
        for patient in self.get_patients():
            avg_centroids += patient.centroids
            avg_volumes += patient.volumes
            avg_distances += patient.distances
            avg_tumor_distances += patient.tumor_distances
            avg_tumor_volume += patient.tumor_volume
        p_avg = {}
        p_avg['centroids'] = avg_centroids / self.num_patients
        p_avg['volumes'] = avg_volumes / self.num_patients
        p_avg['distances'] = avg_distances / self.num_patients
        p_avg['tumor_distances'] = avg_tumor_distances / self.num_patients
        p_avg['tumor_volume'] = avg_tumor_volume / self.num_patients
        #defaults to a dict, adding in a parameter to only look at one thing
        if key == 'all':
            return(p_avg)
        else:
            return(p_avg[key])

    def set_total_dose_prediction(self, predictor):
        #should be like a skikit model, so takes numppy array with .predict(x)
        #and returns a 1d numpy array of y
        self.total_dose_predictor = predictor

    def gen_organ_distance_matrix(self):
        #function to get a matrix I can try some dose prediction on?
        #this will only work if patients actually has a distance value
        features = np.zeros((self.num_patients, 1035))
        feature_names = []
        for x in range(0, Constants.num_organs):
            for y in range(x, Constants.num_organs):
                feature_names.append(Constants.organ_list[x] + '-' + Constants.organ_list[y])
        indices = [np.empty((990,)), np.empty((990,))]
        count = 0
        for row in range(1,45):
            for col in range(row + 1, 45):
                indices[0][count] = row
                indices[1][count] = col
                count += 1
        for patient_idx in range(0, self.num_patients):
            patient = self.patients[patient_idx]
            inds = np.triu_indices(len(patient.distances))
            features[patient_idx, :] = patient.distances[inds].ravel()
        #standarization, not needed for binary trees though
        features = (features - np.mean(features, axis = 0))/(np.std(features, axis = 0) + .00000001)
        return (features, feature_names)
    
    def gen_tumor_distance_matrix(self):
        #function to get a matrix I can try some dose prediction on?
        features = np.zeros((self.num_patients, 45))
        feature_names = Constants.organ_list
        for patient_idx in range(0, self.num_patients):
            patient = self.patients[patient_idx]
            features[patient_idx, :] = patient.tumor_distances
        #standarization, not needed for binary trees though
        features = (features - np.mean(features, axis = 0))/(np.std(features, axis = 0))
        return (features, feature_names)

    def gen_patient_feature_matrix(self):
        #function to get a matrix I can try some dose prediction on?
        features = np.zeros((self.num_patients, 14))
        feature_names = ['gtvp volume', 'gtvn volume', 'prescribed dose', 'total_organ_volume', 
                         'BOT','GPS','Tonsil','NOS', 'gtvp_x', 'gtvp_y', 'gtvp_z', 'Left', 'Right', 'Bilateral']
        laterality_map = {'L': 0, 'R': 1, 'Bilateral': 2}
        subsite_map = {'BOT': 0, 'GPS': 1, 'Tonsil': 2, 'NOS': 3}
        for patient_idx in range(0, self.num_patients):
            patient = self.patients[patient_idx]
            features[patient_idx, 0] = patient.gtvp_volume
            features[patient_idx, 1] = patient.gtvn_volume
            features[patient_idx, 2] = patient.prescribed_dose
            features[patient_idx, 3] = np.sum(patient.volumes)
            features[patient_idx, 4 + subsite_map[patient.tumor_subsite]] = 1
            features[patient_idx, 8:11] = patient.gtvp_position[:]
            features[patient_idx, 11 + laterality_map[patient.laterality]] = 1
        #standarization, not needed for binary trees though
        features = (features - np.mean(features, axis = 0))/(np.std(features, axis = 0))
        return((features, feature_names))

    def evaluate(self, rank_function = 'tumor_organ_ssim', weights = np.array([0,1]), num_matches = 10,
                 td_rank_function = None, td_weights = None):
        #gives a bunch of different metrics for evaluating a given metric
        estimates = self.predict_doses(rank_function, weights, num_matches,
                                       td_rank_function, td_weights)
        differences = self.doses[self.good_rankings, :] - estimates[self.good_rankings, :]
        patient_mean_error = self.labeled_mean_error(differences, axis = 1)
        organ_mean_error = self.labeled_mean_error(differences, axis = 0)
        total_mean_error = np.mean(np.abs(differences))
        total_rmse = np.sqrt(np.mean(differences**2))
        result_dict = {'prediction': estimates,
                       'patient_mean_error': patient_mean_error,
                       'mean_error': total_mean_error,
                       'rmse': total_rmse,
                       'differences': differences,
                       'organ_mean_error': organ_mean_error}
        return(result_dict)
    
    def labeled_mean_error(self, differences, axis):
        #gives us a nice sorted list organ or patient total mean error as a labeled tuple
        error = np.mean(np.abs(differences), axis = axis)
        if axis == 0: #axis 0 is features, so organs here
            labels = Constants.organ_list
        else:
            labels = self.good_rankings #ids for the patients in sorted order?
        name_error_tuples = [ (labels[x], error[x] ) for x in range(0, len(error))]
        name_error_tuples = sorted(name_error_tuples, key = lambda x: x[1])
        return(name_error_tuples)
    
    def set_lower_bound(self, bound, lowerer_bound = None):
        self.score_lower_bound = bound
        if lowerer_bound is not None:
            self.score_lowerer_bound = lowerer_bound

db = PatientSet(patient_set = db, outliers = Constants.v2_bad_entries, score_lowerer_bound = .5)
error_hists = []
bounds = np.linspace(0,.5, 4)
for bound in bounds:
    db.set_lower_bound(bound)
    error_hist, error_matrix = db.run_study(max_matches = 92)
    plt.plot(range(1,len(error_hist) + 1), error_hist, linewidth = 3)
plt.legend(bounds)
#ranks = db.gen_score_matrix(weights = np.array([0,1]), rank_function = 'tumor_organ_ssim')
#best_matches = -np.amin(-ranks, axis = 1)
#optimal_match_count = np.argmin(error_matrix, axis = 1) + 2

#errors = []
#bounds = np.linspace(np.min(ranks), .99*np.max(ranks), 50)
#number_of_outliers = []
#for bound in bounds:
#    db = PatientSet(patient_set = db, score_lower_bound  = bound)
#    diff = db.evaluate()['mean_error']
#    print('lower bound: ', bound, 'error ', diff)
#    errors.append(diff)
#    number_of_outliers.append(len(db.bad_rankings))
#    
#
#plt.plot(bounds, number_of_outliers)

#color_list = []
#for p in db.get_patients():
#    if p.high_throat_dose:
#        color_list.append('b')
#    elif p.full_dose:
#        color_list.append('c')
#    else:
#        color_list.append('r')
#        
#from matplotlib.patches import Patch
#legend = [Patch(facecolor = 'c', edgecolor = 'k', label = 'Bilateral'),
#          Patch(facecolor = 'b', edgecolor = 'k', label = 'High-Throat'),
#          Patch(facecolor = 'r', edgecolor = 'k', label = 'Unilateral')]
#        
#plt.scatter(best_matches, optimal_match_count, color = color_list)
#plt.legend(handles = legend)
#
#error_args = np.argmin(error_matrix, axis = 1) + 2
#bt = []
#unilateral = []
#ht = []
#for p in db.get_patients():
#    if p.high_throat_dose:
#        ht.append(p.pos)
#    elif p.full_dose:
#        bt.append(p.pos)
#    else:
#        unilateral.append(p.pos)
#        
#best_matchcount = {'base': int( np.ceil(np.mean(error_args[bt])) ), 
#                            'unilateral': int( np.ceil(np.mean(error_args[unilateral])) ), 
#                            'high_throat': int( np.ceil(np.mean(error_args[ht])) )}
#
