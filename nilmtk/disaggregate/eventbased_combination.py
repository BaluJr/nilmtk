from __future__ import print_function, division
from collections import OrderedDict, deque
import time
from datetime import datetime
from nilmtk.disaggregate.accelerators import find_steady_states_fast, find_transients_baranskistyle_fast, find_transients_fast, pair_fast, find_sections, myresample_fast, myviterbi_numpy_fast, myviterbi_numpy_faster
from nilmtk import TimeFrame, TimeFrameGroup
from nilmtk.disaggregate import UnsupervisedDisaggregator, UnsupervisedDisaggregatorModel
import numpy as np
import pandas as pd
from functools import reduce, partial
#from multiprocessing import Pool
from sklearn.cluster import KMeans, AgglomerativeClustering, MeanShift, DBSCAN
from collections import Counter
from sklearn_pandas import DataFrameMapper  
import pickle as pckl
from sklearn.metrics import silhouette_score
from sklearn import mixture
from scipy import linalg, spatial
import itertools
from nilmtk.clustering import CustomGaussianMixture
from sklearn.covariance import EmpiricalCovariance, MinCovDet, EllipticEnvelope
import copy
import nilmtk.plots
import pickle
import matplotlib.pyplot as plt 

# Fix the seed for repeatability of experiments
SEED = 42
np.random.seed(SEED)



class EventbasedCombinationDisaggregatorModel(UnsupervisedDisaggregatorModel):        
    '''
    Attributes:
    steady_states: Per phase/sitemeter a list with the steady states (start, end, powerlevel)
    transients:    Per phase/sitemeter a list with the transients (start, delta, end, signature)
    centroids:    meta information centroids from clustering 
    [appliances].events: Per appliance/centroid the dataframe with load for each separated appliance
    params:        Dictionary with the parameters of this Disaggregator. See the default definition
                   below for description of the available paramters.
    '''
    params = {
        # The noise during measuring
        'noise_level': 10, 
        
        # the necessary delta to detect it as a load step
        'state_threshold': 15,

        # variance in power draw allowed for pairing a match
        'min_tolerance': 20, 
        
        # Minimum amount of steps to define it as a steady state
        'min_n_samples':2,
        
        # cols: nilmtk.Measurement, should be one of the following
        


        'cols': [('power','active')], #('power','apparent'), ('power', 'reactive')

        # If transition is greater than large_transition,
        # then use percent of large_transition
        'percent_tolerance': 0.035,
        
        # Lower boundary for a large transition
        'large_transition': 1000,
        
        # Maximal amount of time, an appliance can be on
        'max_on_length' : pd.Timedelta(hours = 2),

        # Take more switch events to enable more complex state machines.
        'max_baranski_clusters': 7,

        # Whether the Hart Algorithms is activated
        'hart_activated' : True,
        
        # Whether the Baranski Algorithms is activated
        'baranski_activated': True,

        # Amount of clusters built for Baranski
        'max_num_clusters': 12,

        # Weights for the optimiyer in baranski
        'gamma1': 0.2,
        'gamma2': 0.2,
        'gamma3': 0.2,

        # How often an event has to appear sothat it is counted as appliance
        'min_appearance': 100,

        # The sample period, in seconds, used for both the
        # mains and the disaggregated appliance estimates.
        'sample_period':120,

        # The frequency of the low res data
        "downsampled_resolution": "5min",

        # Whether spikes are handled in a binary way: Spike or no Spike
        "binary_spikes": False
    }
    

    def extendModels(otherModel):
        '''
        For unsupervised learning, the extend function for the models 
        is even more important as for the supervised/transfer case,
        because each data to disaggregate can be seen as new training data.        

        Not yet implemented.
        '''
        pass



# The following functions are in the global namespace sothat they can be used within the 
# multiprocessing pool.

def add_segments(params):
    ''' 
    Augment the transients by information about the segment 
    
    Paramters
    ---------
    transients: [pd.DataFrame, pd.DataFrame, pd.DataFrame]
        The transients extracted from the original powerflow.
    states: [pd.DataFrame, pd.DataFrame, pd.DataFrame]
        The steady_states extracted from the original powerflow.
    threshold: float
        The threshold earlier used for extracting the segments.
    noise_level: float
        The noiselevel earlier used for extracting the segments.
    binary_spikes: bool
        Whether spikes are handled binary
    
    Returns
    -------
    transients: pd.DataFrame
        The input transients extended by the following columns:
        - segment:  A string identifier, which is the same for 
                    all transients belongin to the same segment.
        - segmentsize: The size of the segment. Amount of transients
                       Inside the section.
          spike:    An additional feature extracted from the signature 
                    of each transient. The highest peak power during 
                    the transient of an upevent / lowest power during 
                    the transients of an downevent.
    '''

    transients, states, threshold, noise_level, binary_spikes = params
    
    # Add segment information
    values = np.array(states.iloc[:,0])
    sections = find_sections((values, noise_level))
    transients['segment'] = pd.Series(data = sections, index = transients.index).shift().bfill()
    transients = transients.join(transients.groupby("segment").size().rename('segmentsize'), on='segment')

    # Don't ask me, why error occured when I do this before the count
    if transients['ends'].dt.tz is None:
        transients['ends'] = transients['ends'].dt.tz_localize('utc')
    transients = transients.reset_index()

    # Additional features, which come in handy
    transients.loc[transients['active transition'] >= 0, 'spike'] = transients[transients['active transition'] >= 0]['signature'].apply(lambda sig: np.cumsum(sig).max())
    transients.loc[transients['active transition'] >= 0, 'spike'] -= transients.loc[transients['active transition'] >= 0, 'active transition']
    transients.loc[transients['active transition'] < 0, 'spike'] =  transients[transients['active transition'] < 0]['signature'].apply(lambda sig: np.cumsum(sig).min())
    transients.loc[transients['active transition'] < 0, 'spike'] -= transients.loc[transients['active transition'] < 0, 'active transition']
    
    if binary_spikes:
        transients.loc[transients['active transition'] >= 0, 'spike'] = (transients.loc[transients['active transition'] >= 0, 'spike'] > 3).astype(int) # 3 Watt is threshhold
        transients.loc[transients['active transition'] < 0, 'spike'] = (transients.loc[transients['active transition'] < 0, 'spike'] < 3).astype(int)
    #else:
    #    transients.loc[transients['active transition'] >= 0, 'spike'] = transients.loc[transients['active transition'] >= 0, 'spike'].clip(lower=0)
    #    transients.loc[transients['active transition'] < 0, 'spike'] = transients.loc[transients['active transition'] < 0, 'spike'].clip(upper=0)

    return transients



def fast_groupby(df, plotting_path, segsize):
    '''
    Does a fast groupby as used for creating the 3-size and 4-size events.
    By unsing numpy it is faster than the pandas version. 
    The fields to incoporate are taken as needed for our disaggregation.
    Returns runs of an appliance.

    Parameters
    ----------
    df: pd.DataFrame
        Pandas DataFrame to group
    plotting_path: str or False
        The path where the plotting shall take place
    segsize: The segmentsize which is targeted

    Returns
    -------
    df: pd.DataFrame.  T
        The grouped dataframe, which can then used as an input for the clustering.
    '''
    
    if len(df) < 4:
        if segsize == 2:
            if plotting_path:
                return pd.DataFrame( columns=['transition_avg', 'spike_up'])
            else:
                return pd.DataFrame(columns=['transition_avg', 'transition_delta', 'spike_up', 'spike_down', 'duration'])
        elif segsize == 3:
            return pd.DataFrame(columns=['fst', 'sec', 'trd', 'fst_spike', 'sec_spike', 'trd_spike', ])
        else:
            return pd.DataFrame(columns=['fst', 'sec', 'trd', 'fth', 'fst_spike', 'sec_spike', 'trd_spike', 'fth_spike', 'fst_duration', 'sec_duration', 'trd_duration'])

        print("few elements to group")


    df = df[['segment', 'active transition', 'starts', 'ends', 'spike']]
    df = df.sort_values(['segment','starts'])
    df['duration'] = (df['starts'] - df.shift()['ends']).dt.seconds
    df.drop(columns=['starts', 'ends'], inplace=True)
    values = df.values
    keys, values = values[:,0], values[:,1:]
    ukeys,index=np.unique(keys, return_index = True) 
    result = []

    # Twoevents
    if segsize == 2:
        if plotting_path:
            for arr in np.vsplit(values,index[1:]):
                result.append(np.array([np.sum(np.abs(arr[:,0]))/2, arr[0,1]]))
            cols = ['transition_avg', 'spike_up']
        else:
            for arr in np.vsplit(values,index[1:]):
                result.append(np.array([np.sum(np.abs(arr[:,0]))/2, arr[0,0] + arr[1,0], arr[0,1], arr[1,1], arr[1,2]]))
            cols = ['transition_avg', 'transition_delta', 'spike_up', 'spike_down', 'duration']
    # Threeevents
    elif segsize == 3:
        for arr in np.vsplit(values,index[1:]):
            result.append(np.concatenate([arr[:,0], arr[:,1]]))#, arr[:,2]]))#, arr[1:,3]]))
        cols = ['fst', 'sec', 'trd', 'fst_spike', 'sec_spike', 'trd_spike', ]  #, 'fst_length', 'sec_length', 'trd_length']#, 'fst_duration', 'sec_duration']
    # Fourevents
    else:
        for arr in np.vsplit(values,index[1:]):
            result.append(np.concatenate([arr[:,0], arr[:,1], arr[1:,2]])) # arr[:,2]]))#, arr[1:,3]]))
        cols = ['fst', 'sec', 'trd', 'fth', 'fst_spike', 'sec_spike', 'trd_spike', 'fth_spike', 'fst_duration', 'sec_duration', 'trd_duration'] #'fst_length', 'sec_length', 'trd_length', 'fth_length']#, 'fst_duration', 'sec_duration', 'trd_duration']
    df2=pd.DataFrame(index = ukeys, data = result, columns=cols)
    return df2



def fast_groupby_with_additional_grpfield(df, plotting_path):
    '''
    Special version of the fast groupby, that is used for creating the 2-size events from the 
    4 size events. In this case the additional group-field has to be incorporated into the grouping.
    By unsing numpy it is faster than the pandas version. 
    The fields to incoporate are taken as needed for our disaggregation.
    Returns runs of an appliance.

    Parameters
    ----------
    df : pd.DataFrame
        Pandas DataFrame to group
    plotting_path: str or False
        The path where the plotting shall take place

    Returns
    -------
    pd.DataFrame.  The grouped dataframe which can be used as an input for the clustering.
    '''

    df = df[['segment', 'grp', 'active transition', 'starts', 'ends', 'spike']]
    df = df.sort_values(['segment','grp', 'starts'])
    df['duration'] = (df['starts'] - df.shift()['ends']).dt.seconds
    values = df.drop(columns=['starts', 'ends']).values
    keys, values = values[:,:2], values[:,2:]
    ukeys,index=np.unique(keys[:,0] + keys[:,1].astype(str), return_index = True) 
    result = []
    
    # This version only supports twoevents    
    if plotting_path:
        for arr in np.vsplit(values,index[1:]):
            result.append(np.array([np.sum(np.abs(arr[:,0]))/2, arr[0,1]]))
        cols = ['transition_avg', 'spike_up']
    else:
        for arr in np.vsplit(values,index[1:]):
            result.append(np.array([np.sum(np.abs(arr[:,0]))/2, arr[0,0] + arr[1,0], arr[0,1], arr[1,1], arr[1,2]]))
        cols = ['transition_avg', 'transition_delta', 'spike_up', 'spike_down', 'duration']
    idx = pd.MultiIndex.from_arrays([df.loc[::2,'segment'],df.loc[::2,'grp'].values])
    df2 = pd.DataFrame(index = idx, data = result, columns=cols)
    return df2



def myviterbi_numpy(segment, appliances):
    '''
    Simplified version of the viterbi algorithm, which is used to identify the 
    known appliances within the longer segments.

    Paramters
    ---------
    segment: pd.DataFrame
        DataFrame of the transients within the current segment
    appliances: [dic, ...]
        Definition of the appliances. Defined within a dictionary. The dictionary 
        contains the following properties:
        - 1..n_events: For each event within the signature of the appliance as a
                       sklearn.covariance.EllipticEnvelope.
        - length:      length of the appliance signature (2, 3 or 4)
        - subtype:     The subtype of the appliance
        - appliance:   The appliance label this appliance is
        The last 3 properties are needed to map back to the original transient 
        dataframe.
    
    Returns
    -------
    labels: np.ndarray<int>
        For each event in the segment the appliance it belongs to. BUT pay attention
        this label is relative to the appliances dictionary the function gets as an
        input. To get the real segmentsize-subtype-appliance triple, one has to get 
        it from the appliance dictionaries first.
    '''
    
    # For each event the mapping to which statemachine-step it may belong
    event_to_statechange = [[] for i in range(len(segment))]
    rows = segment[['active transition','spike']]
    for i, appliance in enumerate(appliances):
        for step in range(appliance['length']):
            valids = appliance[step].predict(rows.values) == 1   # -1 means outlier
            for j, valid in enumerate(valids):
                if valid:
                    event_to_statechange[j].append((i, step))

    # Create the lookup for Matrices: T1=Distances, T2=Matches, T3=Path
    idx = pd.IndexSlice
    multindex = pd.MultiIndex(levels=[[] for _ in range(len(appliances))],
                            labels=[[] for _ in range(len(appliances))],
                            names=range(len(appliances)))
    state_table = pd.DataFrame(index = multindex, columns = ['T1', 'T2', 'T3'])
    startstate = tuple(np.zeros(len(appliances)))
    state_table.loc[startstate,:] = [0, 0, ""]
        
    # Find the best path
    t0 = time.time()
    for i, transient in segment.iterrows():
        i = transient['segplace']

        # Do the calculation for this step
        new_state_table = state_table.copy()
        for appliance, step in event_to_statechange[i]:
            # Create retrieval for all fitting available states in statestable
            lookup = [slice(None) for _ in range(appliance)]
            lookup.append([step])
            rows = state_table.loc[tuple(lookup),:]
            if len(rows) == 0:
                continue

            # Calculate the steps
            cost = spatial.distance.cdist([transient[['active transition','spike']]], np.expand_dims(appliances[appliance][step].location_, 0), 'mahalanobis', VI=linalg.inv(appliances[appliance][step].covariance_)).squeeze()
            #newappliancestate = (step+1 % appliances[appliance]['length'])
            #tst = lambda e, a, ast: tuple(e[0][:a] + (ast,) + e[0][a+1:])
            #newstate = np.apply_along_axis(tst, 0, rows.index.values, a = appliance, ast=newappliancestate)
            #newstate = np.array(*rows.index.values).reshape(-1, len(appliances))
            newstate = np.array([list(e) for e in rows.index.values])
            newstate[:,appliance] = (step+1) % (appliances[appliance]['length'])
            tmp  = list(map(tuple, newstate))
            newstate = np.zeros(len(newstate), dtype='object')
            newstate[:] = tmp 

            # Insert when not availbale yet
            new_introduced = pd.MultiIndex.from_tuples(newstate).difference(new_state_table.index)
            #new_state_table.loc[new_introduced,:] = [0, 1e100, ""] # Somehow bug in Pandas
            new_state_table = new_state_table.append(pd.DataFrame(data = {'T1':1e100, 'T2':0, 'T3':''}, index = new_introduced))

            # Update when better
            isnewmatch = (step == appliances[appliance]['length']-1)
            to_update = ((rows['T2'].values + isnewmatch > new_state_table.loc[newstate,'T2'].values) | ((rows['T2'].values == new_state_table.loc[newstate,'T2'].values) & ((rows['T1'].values + cost)< new_state_table.loc[newstate,'T1'].values))) # more matches or less cost
            to_update_in_new = newstate[to_update]
            new_state_table.loc[to_update_in_new,'T1'] = rows.loc[to_update,'T1'].values + cost
            new_state_table.loc[to_update_in_new,'T2'] = rows.loc[to_update,'T2'].values + isnewmatch
            new_state_table.loc[to_update_in_new,'T3'] = rows.loc[to_update,'T3'].values + ";" + str(i) + "," + str(appliance)
        state_table = new_state_table

    # The best path which ends in zero state is result
    T1, T2, T3 = state_table.loc[startstate]
    labels = [-1] * len(segment)
    for cur in T3.split(";")[1:]:
        location, appliance = eval(cur)
        labels[location] = appliance
    return labels



def remove_inconfident_elements(transients):
    '''
    Remove elements which are insecure.
    Not used at the moment.
    '''
    shorten_segment = lambda seg: seg[:seg.rfind('_', 0, -2)]
    new_segments = transients[~transients['confident']]['segment'].apply(shorten_segment)
    transients[~transients['confident']]['new_segments'] = new_segments 
    transients.drop(columns=['segmentsize'], inplace = True)
    return transients.join(transients.groupby("segment").size().rename('segmentsize'), on='segment')



def separate_simultaneous_events(transients, steady_states, noise_level):
    '''
    This function finds the common transients in the list of transients.
    The equal ones are removed from the timelines and then returned as 
    a dedicated timeline.
    As there are curently no appliances connected to all three phases, only two
    phase connected events are considered.

    Paramters
    ---------
    transients: [pd.DataFrame, pd.DataFrame, pd.DataFrame]
        The steady_states where the multiphase appearances are removed from.
    steady_states: [pd.DataFrame, pd.DataFrame, pd.DataFrame]
        The steady_states where the multiphase appearances are removed from.
    overall_powerflows: [pd.DataFrame, pd.DataFrame, pd.DataFrame]
        Has already the 5min resolution.
        The rest powerflows. The events which are separated into the separate 
        load profiles have to be subtracted from the overall_powerflows.
    noise_level: float
        The noiselevel earlier used for extracting the segments.
    
    Returns
    -------
    transients: [pd.DataFrame, pd.DataFrame, pd.DataFrame, ...]
        The transients where the multiphase sections are removed from the original three
        DataFrames and added to separate new DataFrames.
    steady_states: [pd.DataFrame, pd.DataFrame, pd.DataFrame, ...]
        The transients where the multiphase sections are removed from the original three
        DataFrames and added to separate new DataFrames.
    '''

    plotting = False
    if plotting:
        original_transients = copy.deepcopy(transients)
        interesting_sections = []
    # When only one powerflow, no simultaneous events
    if len(transients) <= 1:
        return
    new_transients = []   
    new_steady_states =[]
        
    # Events over all phases not yet supported
    common_transients = pd.concat(transients, join='inner', axis=1)
    transitions = common_transients['active transition']
    abc_same_size = transitions.std(axis=1) < 0.1 * transitions.mean(axis=1)
    if not common_transients.empty and (abc_same_size.sum() / len(common_transients)) > 0.1:
        raise Exception('There is a three phase appliance, which is not yet supported.')
                
    # Find simultaneous events between the pairs of phases
    for a, b in [(0,1),(0,2),(1,2)]:
        common_transients = pd.concat([transients[a], transients[b]], join='inner', axis=1)
        common_transients = \
            common_transients[common_transients["active transition"].abs().sum(axis=1)
                              == common_transients["active transition"].sum(axis=1).abs()]
        if len(common_transients) < 10:
            continue
        
        #nilmtk.plots.latexify()
        #pd.concat(map(lambda df: df['active transition'], transients), axis=1).fillna(0).cumsum().plot()
        #plt.show()

        # Find regions where common powersource active (Only oven on multiple lines -> Pattern)
        to_cluster = (common_transients.index - pd.Timestamp("1.1.2000", tz="UTC")).total_seconds()
        to_cluster = np.expand_dims(to_cluster.values, 1)
        dbs = DBSCAN(eps=300, min_samples=5)
        labels = dbs.fit_predict(to_cluster)
        common_transients['labels'] = labels
        grps = common_transients.reset_index()[['starts', 'labels']].groupby('labels')
            
        # Add the powerflows in between
        firsts = grps.first()
        lasts = grps.last()
        confident_common_transients = [pd.DataFrame(), pd.DataFrame(), pd.DataFrame()]
        # The -1 Group is removed by starting from second index
        for fst, lst in zip(firsts.values[1:],lasts.values[1:]):
            for i in [a,b]:
                # Use sections to find elements, which belong together
                cur = transients[i][fst:lst]
                prev_value = steady_states[i].iloc[transients[i].index.get_loc(fst)-1]
                values = np.append(np.array(prev_value[0]), steady_states[i][fst:lst].iloc[:,0])
                sections = pd.Series(find_sections((values, noise_level))).shift()[1:] # First was added before
                invalids = cur.index[(sections == "")]
                cur.drop(invalids, inplace = True)
                if cur.empty:
                    continue
                
                # Correct smaller aberations and add new 
                sum = cur.sum()['active transition']
                cur.loc[cur.index[-1], 'active transition'] -= sum
                confident_common_transients[i] = confident_common_transients[i].append(cur) #.add_suffix(str(i))
                confident_common_transients[i]['from' + str(i)] = confident_common_transients[i]['active transition']

                # Adapt the original powerflow sothat removal invisible in real powerflow
                steady_states[i].drop(cur.index, inplace = True)
                transients[i].drop(cur.index, inplace = True)
                transients[i].loc[invalids,'active transition'] = steady_states[i].loc[invalids,'active average'] - steady_states[i].shift(1).loc[invalids,'active average']

                if plotting:
                    interesting_sections.append(TimeFrame(fst,lst))
        # Create the new events 
        common_transients = pd.concat(confident_common_transients, axis=1)
        if len(common_transients) == 0:
            continue

        common_transients['active transition'] = common_transients['active transition'].sum(axis=1)
        signatures = common_transients.loc[:,'signature']
        common_transients = common_transients.loc[:,~common_transients.columns.duplicated()]
        up = common_transients['active transition'] > 0
        common_transients.loc[up,'signature'] = signatures.loc[up,'signature'].fillna(0).applymap(lambda sig: np.max(np.cumsum(sig))).sum(axis=1)
        common_transients.loc[~up,'signature'] = signatures.loc[~up,'signature'].fillna(0).applymap(lambda sig: np.min(np.cumsum(sig))).sum(axis=1)
        
        # Set the multiphase events immediately as secure events
        common_transients['appliance'] = 0
        common_transients['confident'] = True
        common_transients['subtype'] = 0
        
        # End is set to nonsense here as it is not used later on either way
        common_transients['ends'] = common_transients.index + pd.Timedelta('4s')

        # Create and add the new transients 
        new_transients.append(common_transients)
        new_states = pd.DataFrame(common_transients['active transition'].cumsum())
        new_states.rename(columns={'active transition':'active average'}, inplace = True)
        new_steady_states.append(new_states)

    if plotting:
        nilmtk.plots.plot_multiphase_event(original_transients, transients, new_transients, interesting_sections[0])

    # Put together the new results
    transients.extend(new_transients)
    steady_states.extend(new_steady_states)

    return transients, steady_states

def my_resample_highdef(power, start, end, resolution = '5min', columns = ["active transiton"]):
    '''
    This function is used to resample high def samples to a reasonable 
    resolution by a weighted mean.
    Paramters
    ---------
    power: pd.Series
        The power of the appliance as transients with the flags.
    start: pd.TimeStampe
        Beginning of the region to downsample.
    end: pd.TimeStampe
        End of the region to downsample.
    resolution: string
        The resolution to downsample to.
    columns: string
        Name of columns of the resulting DataFrame

    Returns
    -------
    appliance:
        Power of the appliance downsampled to the given resolutions
    '''
    appliance = power.cumsum()
    new_idx = pd.DatetimeIndex(start = start, end = end, freq = resolution).tz_convert('utc')
    newSeries = pd.Series(index = new_idx)
    resampledload = appliance.append(newSeries).sort_index()
    resampledload = resampledload.ffill().bfill()
    appliance = pd.DataFrame(resampledload.resample(resolution, how=myresample_fast), columns=columns)
    appliance = appliance.fillna(0)
    return appliance

def find_appliances(params):
    ''' Assign labels to the transients, defining the appliances they belong to.

    Paramters
    ---------
    transients: pd.DataFrame
        The transients which are processed and clustered to assign them to appliances
    state_threshold: float
        The threshhold earlier used for extracting the segments.
    plotting_path: str or False
        If and where to store plots

    Returns
    -------
    transients: pd.DataFrame
        The input transients extended by the following columns:
        - subtype:  A field defining the clusterer, that is responsible for the assignment
                    to the appliance
        - appliance: the appliance the transient belongs to
        - confident: If set to true, one can be confident, that the clustering is reasonable.
    clusterer: dic<str, pd.GaussianMixture>
        Dictionary which contains for each subtype the corresponding GaussianMixture
        used during clustering.
    '''
    try:
        transients, state_threshold, plotting_path = params
    except:
        transients, state_threshold = params
        plotting_path = False
    clusterers = {}
    
    # Separate the two-flag events 
    twoevents = transients[transients['segmentsize'] == 2]
    twoevents = fast_groupby(twoevents, plotting_path, 2)
    new_clusterers, subtypes, labels, confidences = \
        _gmm_clustering_hierarchical(twoevents, "2", max_num_clusters = [6, 4], dim_scaling = {'transition_avg':3},
                                     check_for_variance_dims=['transition_avg'], single_only=True)
    clusterers = {**clusterers, **new_clusterers}
    twoevents['confident'] = confidences
    twoevents['appliance'] = labels
    twoevents['subtype'] = subtypes
    if plotting_path:
        nilmtk.plots.latexify(fontsize=11)
        nilmtk.plots.plot_clustering(clusterers, twoevents, ["transition_avg", "spike_up"], s=0.5)
        plt.tight_layout()
        plt.savefig(plotting_path + "\disag_clusterer.pdf")
    transients = transients.join(twoevents[['appliance', 'confident', 'subtype']], on="segment")
    transients['appliance'] = transients['appliance'].fillna(-1)
    transients['confident'] = transients['confident'].fillna(0).astype(bool)
    transients['subtype'] = transients['subtype'].fillna(0)
    #transients = remove_inconfident_elements(transients)

    ## Separate the three-flag events
    threeevents = transients[transients['segmentsize'] == 3]
    threeevents = fast_groupby(threeevents, plotting_path, 3)
    threeevents['subtype'] = (threeevents['sec'] < 0).astype(int)
    threeevents['appliance'] = -1
    threeevents['confident'] = False
    for type in range(2):
        name = "3_" + str(type)
        cur_threeevents = threeevents[threeevents['subtype'] == type].drop(columns=['subtype', 'appliance', 'confident'])
        new_clusterers, subtypes, labels, confidences = \
            _gmm_clustering_hierarchical(cur_threeevents, name ,max_num_clusters = 10,
                                         check_for_variance_dims=['fst', 'sec', 'trd'], single_only=True)
        clusterers = {**clusterers, **new_clusterers}
        threeevents.loc[threeevents['subtype'] == type, 'confident'] = confidences
        threeevents.loc[threeevents['subtype'] == type, 'appliance'] = labels
        threeevents.loc[threeevents['subtype'] == type, 'subtype'] = subtypes
    transients = transients.join(threeevents[['appliance','subtype', 'confident']], on="segment", rsuffix="three")
    transients.update(transients[['appliancethree', 'confidentthree', 'subtype']].rename(columns={'appliancethree':'appliance', 'confidentthree':'confident', 'subtypethree': 'subtype'}))
    transients.drop(["appliancethree", "confidentthree", "subtypethree"],axis=1, inplace = True)
    transients['confident'] = transients['confident'].astype(bool) # whyever this is needed
    #transients = remove_inconfident_elements(transients)


    ## Separate the four-flag events
    allfourevents = transients[transients['segmentsize']==4]

    ## First look, whether just overlapping or sequential twoevents
    fourevents = fast_groupby(allfourevents, plotting_path, 4)
    overlapping = (fourevents['fst']>0) & (fourevents['sec']>0) & (fourevents['trd']<0) & (fourevents['fth']<0)  
    d1 = (fourevents['fst'] + fourevents['trd']).abs() + (fourevents['sec'] + fourevents['fth']).abs()
    d2 = (fourevents['fst'] + fourevents['fth']).abs() + (fourevents['sec'] + fourevents['trd']).abs()
    fourevents['overlap1'] = overlapping & (d1 <= d2)
    fourevents['overlap2'] = overlapping & (d2 < d1)
    fourevents['sequential'] = ((fourevents['fst'] + fourevents['sec']) < state_threshold)
    allfourevents = allfourevents.join(fourevents[['overlap1','overlap2','sequential']], on='segment')
            
    allflank = allfourevents[allfourevents['overlap1']].sort_values(['segment','starts']) # starts for easier debugging
    allflank['grp'] = np.array([0,1,0,1]*(len(allflank)//4)) + np.outer(range(len(allflank)//4),np.array([2,2,2,2])).flatten()
    flank = allfourevents[allfourevents['overlap2']].sort_values(['segment','starts'])
    flank['grp'] = np.array([0,1,1,0]*(len(flank)//4)) + np.outer(range(len(flank)//4),np.array([2,2,2,2])).flatten()
    allflank = allflank.append(flank)
    flank = allfourevents[allfourevents['sequential']].sort_values(['segment','starts'])
    flank['grp'] = np.array([0,0,1,1]*(len(flank)//4)) + np.outer(range(len(flank)//4),np.array([2,2,2,2])).flatten()
    allflank = allflank.append(flank)

    possible_twoevents = fast_groupby_with_additional_grpfield(allflank, plotting_path)
    labels, confidence, subtypes =  _gmm_hierarchical_predict_and_confidence_check("2", possible_twoevents, clusterers, ['transition_avg'])
    possible_twoevents['confident'] = confidence
    possible_twoevents['appliance'] = labels
    possible_twoevents['subtype'] = subtypes
    allflank = allflank.drop(['appliance','confident', 'subtype'],axis=1).join(possible_twoevents[['appliance', 'confident', 'subtype']], on=["segment",'grp'])
    # Only keep 4 events where both fitted to twoevent
    allflank = allflank.drop(columns=['confident']).join(allflank[['segment','confident']].groupby('segment').all(), on='segment')
    
    allflank.loc[allflank['confident'],'segmentsize'] = 2   # Really 2-events
    allflank.loc[~allflank['confident'],'grp'] = -1         # All non confident elements are one 4 group per segment 
    transients = transients.join(allflank[['appliance','segmentsize', 'grp']], rsuffix = '_new')
    transients.update(transients[['appliance_new']].rename(columns={'appliance_new':'appliance'}))
    transients.update(transients[['segmentsize_new']].rename(columns={'segmentsize_new':'segmentsize'}))
    transients.drop(["appliance_new", "segmentsize_new"], axis=1, inplace = True)
    transients['grp'] = transients['grp'].fillna(0)
    
    # All other events are fourevents
    fourevents = fourevents.join(allflank.groupby('segment').first()['confident']).fillna(False)
    fourevents['subtype'] = ((fourevents['sec'] > 0).astype(int) + (fourevents['trd'] > 0).astype(int)*2,0)[0]
    rest_fourevents = fourevents[~fourevents['confident'].astype(bool)] 
    for type in range(4):
        name = "4_" + str(type)
        events_to_cluster = rest_fourevents[rest_fourevents['subtype'] == type].dropna(axis=1)
        new_clusterers, subtypes, labels, confidences = \
            _gmm_clustering_hierarchical(events_to_cluster, name, max_num_clusters = 10,
                                         check_for_variance_dims=['fst', 'sec', 'trd', "fth"], single_only=True)
        clusterers = {**clusterers, **new_clusterers}
        rest_fourevents.loc[rest_fourevents['subtype'] == type, 'confident'] = confidences
        rest_fourevents.loc[rest_fourevents['subtype'] == type, 'appliance'] = labels
        rest_fourevents.loc[rest_fourevents['subtype'] == type, 'subtype'] = subtypes
    transients = transients.join(rest_fourevents[['appliance', 'subtype', 'confident']], on="segment", rsuffix="four")
    transients.update(transients[['appliancefour',  'subtypefour', 'confidentfour']].rename(
        columns={'appliancefour':'appliance', 'subtypefour':'subtype', 'confidentfour':'confident'}))
    transients.drop(["appliancefour", 'subtypefour', 'confidentfour'],axis=1, inplace = True)


    # Build identified appliances (outlier detections for the events of the appliances, and remove too small)
    transients['segplace'] = transients.groupby(['segment', 'grp']).cumcount()
    appliances = []
    for (length, subtype, appliance), rows in transients[(transients['segmentsize']<=4)
            & transients['confident']].groupby(['segmentsize', 'subtype', 'appliance']):
        cur_appliance = {'length':int(length), 'appliance':appliance, 'subtype': subtype}
        if len(rows) // length < 5 or length == 4:
            transients.loc[(transients['segmentsize'] == length) & (transients['subtype'] == subtype) & (transients['appliance'] == appliance), 'confident'] = False
        infostr = "__"
        for place, concrete_events in rows.groupby('segplace'):
            cur_appliance[place] = EllipticEnvelope().fit(concrete_events[['active transition','spike']])
            infostr += str(cur_appliance[place].location_[0]) + "_"
        cur_appliance['aaa'] = infostr + "(" + str(len(rows) // length) + ")__"
        appliances.append(cur_appliance)
    print("##################### " + str(len(appliances)))

    # Now find the appliances within the longer segments
    #all_segments = str(len(transients[transients['segmentsize'] > 4]['segment'].unique()))
    #segi = 1
    #t0 = time.time()
    #for segmentid, segment in transients[transients['segmentsize'] > 4].groupby('segment'):
    #    labels = myviterbi_numpy_fast(segment[['active transition', 'spike']].values, appliances)

    #    # Translate labels and update transients
    #    reallabels = pd.DataFrame(columns=['segmentsize','subtype','appliance'], index = transients.loc[transients['segment'] == segmentid].index)
    #    for i, lbl in enumerate(labels):
    #        a = appliances[lbl]
    #        reallabels.iloc[i,:] = [a['length'], a['subtype'], a['appliance']]
    #    transients.loc[transients['segment'] == segmentid, ['segmentsize','subtype','appliance']] = reallabels
    #    print("Original: " + str(segi) + "/" + all_segments + ": " + str(time.time()-t0) + "|| Avg: " + str((time.time()-t0)/segi))
    #    segi += 1
    #    if segi == 20:
    #        break
    
    print("##########")
    all_segments = str(len(transients[transients['segmentsize'] > 4]['segment'].unique()))
    segi = 1
    t0 = time.time()
    for segmentid, segment in transients[transients['segmentsize'] > 4].groupby('segment'):
        if segi == 1:
            segi += 1
            continue
        labels = myviterbi_numpy_faster(segment[['active transition', 'spike']].values.astype(np.float32), appliances)

        # Translate labels and update transients
        reallabels = pd.DataFrame(columns=['segmentsize','subtype','appliance'], index = transients.loc[transients['segment'] == segmentid].index)
        for i, lbl in enumerate(labels):
            a = appliances[lbl]
            reallabels.iloc[i,:] = [a['length'], a['subtype'], a['appliance']]
        transients.loc[transients['segment'] == segmentid, ['segmentsize','subtype','appliance']] = reallabels
        if segi % 10 == 0:
            print("Faster: " + str(segi) + "/" + all_segments + ": " + str(time.time()-t0) + "|| Avg: " + str((time.time()-t0)/segi))
        segi += 1

    transients['segmentsize'] = transients['segmentsize'].astype(int)
    return {'transients':transients, 'clusterer':clusterers}



def create_appliances(params):
    ''' Creates the appliances from the labeled events.

    Paramters
    ---------
    transients: pd.DataFrame
        The transients among which appliances are searched.
    overall_powerflow: pd.DataFrame
        The overall_powerflow with 5min resolution, which was generated during creation.
    min_appearance: int
        How many events an appliance must have to be persisted.
    exact_nilm_datastore: bool 
        When set to true, also return the full resolution result.
    
    Returns
    -------
    appliances: pd.DataFrame
        Powerflow of the succesfully disaggregated appliances.
        Currently a resolution of 5 minutes.
    overall_powerflow: pd.DataFrame
        The rest powerflow which remains after summing up all successfully disaggregated appliances.
        Currently a resolution of 5 minutes.
    exact_nilm: Optional
        If 'exact_nilm_datastore' is set to true, then this field contains a dataframe which stores 
        the flags at there exact point in time.
    '''

    transients, overall_powerflow, min_appearance, exact_nilm_datastore = params
    appliances = []
    if exact_nilm_datastore:
        exact_appliances = []

    for (size, subtype, appliance), group in transients[transients['confident']].groupby(['segmentsize','subtype','appliance']):
        print(str(size) + "-" + str(appliance))
        if appliance == -1 or size == 1 or (len(group) / size) < 5:
            continue

        # Filter out overlapping events (Merge fitting and remove too short sections)
        group['switches'] = (group['segment'] != group.shift()['segment']).cumsum()
        group = group.join(group.groupby('switches').size().rename('switchsize'), on='switches')
        group = group[group['switchsize'] == group['segmentsize']] 
                
        # Correction of errors
        power = group.set_index('starts')['active transition'] # Should be already sorted
        error = power.groupby(np.outer(range(len(group)//size), np.ones(size)).flatten().astype(int)).sum()
        power.update(power[::size] - error.values.flatten())
        
        # If demanded also return a full resolution output by using the flags.
        if exact_nilm_datastore:
            power_detailed = power.append(pd.Series(0, name='power active', index=power.index - pd.Timedelta('0.5sec')))
            power_detailed = pd.DataFrame(power_detailed)
            power_detailed.columns = overall_powerflow.columns
            power_detailed = power_detailed.sort_index().cumsum()
            #power_detailed.loc[power_detailed.index[-1] + pd.Timedelta('0.5sec'),:] = [0]
            power_detailed = pd.DataFrame(power_detailed.astype(np.float32))
            power_detailed.columns = pd.MultiIndex.from_tuples([('power', 'active')], names=['physical_quantity', 'type'])
            exact_appliances.append(power_detailed)
            
        # Default output: Resample the appliances to 5min (weighted mean)
        appliance = my_resample_highdef(power, overall_powerflow.index[0],  overall_powerflow.index[-1], resolution = '5min', columns=pd.MultiIndex.from_tuples([('power', 'active')], names=['physical_quantity', 'type']))
        #appliance = power.cumsum()
        #new_idx = pd.DatetimeIndex(start = overall_powerflow.index[0], end = overall_powerflow.index[-1], freq = '5min').tz_convert('utc')
        #newSeries = pd.Series(index = new_idx)
        #resampledload = appliance.append(newSeries).sort_index()
        #resampledload = resampledload.ffill().bfill()
        #appliance = pd.DataFrame(resampledload.resample('5min', how=myresample_fast), columns=overall_powerflow.columns)
        #appliance = appliance.fillna(0)

        # Prepare output and subtract from overallpowerflow
        appliances.append(appliance)
        overall_powerflow.columns = pd.MultiIndex.from_tuples([('power', 'active')], names=['physical_quantity', 'type'])
        overall_powerflow  = pd.eval('overall_powerflow - appliance')

    if exact_nilm_datastore:
        return { 'appliances':appliances, 'overall_powerflow':overall_powerflow,  "exact_nilm": exact_appliances}
    else:
        return { 'appliances':appliances, 'overall_powerflow':overall_powerflow }




def create_multiphase_appliances(params):
    ''' Creates the appliances from the multiphases phases. 
    At the moment all contained transients are expected to belong for 
    sure to a single component.
    The overall_powerflows, which contain the rest for the original 
    phases are reduced by the amount ending up in the multiphase events.

    Paramters
    ---------
    transients: pd.DataFrame
        The transients among which appliances are searched.
    overall_powerflows: pd.DataFrame
        The overall_powerflows with 5min resolution. They are reduced by the amount 
        contained inside the multiphase events.
    exact_nilm_datastore: bool 
        When set to true, also return the full resolution result.
    
    Returns
    -------
    appliances: pd.DataFrame
        Powerflow of the succesfully disaggregated appliances.
        Currently a resolution of 5 minutes.
    overall_powerflow: pd.DataFrame
        The rest powerflow which remains after summing up all successfully disaggregated appliances.
        Currently a resolution of 5 minutes.
    exact_nilm: Optional
        If 'exact_nilm_datastore' is set to true, then this field contains a dataframe which stores 
        the flags at there exact point in time.
    '''

    transients, overall_powerflow, exact_nilm_datastore = params
    appliances = []
    if exact_nilm_datastore:
        exact_appliances = []

    # If demanded also return a full resolution output by using the flags
    power = transients['active transition']
    if exact_nilm_datastore:
        power_detailed = power.append(pd.Series(0, name='power active', index=transients.index - pd.Timedelta('0.5sec')))
        power_detailed = pd.DataFrame(power_detailed)
        power_detailed.columns = overall_powerflow[0].columns
        power_detailed = power_detailed.sort_index().cumsum()
        power_detailed.loc[power_detailed.index[-1] + pd.Timedelta('0.5sec'),:] = [0]
        exact_appliances.append(pd.DataFrame(power_detailed.astype(np.float32)))
            
    # Default output: Resample the appliances to 5min (weighted mean)
    # Find the two transitions and remove from the overall_powerflows they belong to
    columns = transients.columns
    appliances = []
    for col in columns:
        if not col.startswith('from'):
            continue
        phase = int(col[-1])
        cur = transients[col]
        appliance = my_resample_highdef(cur, overall_powerflow[0].index[0],  overall_powerflow[0].index[-1], resolution = '5min', columns=overall_powerflow[0].columns)
        if len(appliances) == 0:
            appliances.append(appliance)
        else:
            appliances[-1] += appliance
        overall_powerflow[phase] -= appliance

    op = overall_powerflow[0]
    overall_powerflow.append(pd.DataFrame(index = op.index, columns = op.columns, data=0))
    if exact_nilm_datastore:
        return { 'appliances':appliances, 'overall_powerflow':overall_powerflow,  "exact_nilm": exact_appliances}
    else:
        return { 'appliances':appliances, 'overall_powerflow':overall_powerflow }


def _gmm_clustering(events, max_num_clusters=5, exact_cluster=None, dim_scaling = {}, dim_emph = {}):
    '''The core clustering method.
    Does the clustering by using gaussian mixture models.

    Paramters
    ---------
    dim_scaling:
        Scaling of certain dimensions of the input. Won't affect GMM but the k-means initialization.
    dim_emph:
        Increase importance of certain dimensions during E-step of EM-algorithm. All other dimensions'
        covariances are scaled by this value.
    '''

    # Special case:
    if len(events) < 20: # len(events.columns):
        return None, (np.ones(len(events))*-1)

    # Preprocess dataframe
    mapper = DataFrameMapper([(column, None) for column in events.columns])
    clustering_input = mapper.fit_transform(events.copy())

    # Find scaling dimensions (makes a difference for kmeans-init)
    scaling_dims = list(dim_emph.keys())
    indices = np.in1d(events.columns, scaling_dims)
    clustering_input[:,indices] *= list(dim_scaling.values())
    # Translate dim_emph into the positions
    emph_dims = list(dim_scaling.keys())
    indices = np.in1d(events.columns, emph_dims)
    dim_emph = dict(zip(indices, list(dim_emph.values())))

    # Do exact clustering if demanded
    if not(exact_cluster is None):
        gmm = CustomGaussianMixture(n_components=exact_cluster, covariance_type='full', n_init = 10, dim_emph = dim_emph)
        gmm.fit(clustering_input)
        return gmm, gmm.predict(clustering_input)

    # Do the clustering
    best_gmm = CustomGaussianMixture(n_components=1, covariance_type='full', n_init = 5, dim_emph = dim_emph)
    best_gmm.fit(clustering_input)
    best_bic = best_gmm.bic(clustering_input)
    for n_clusters in range(2, max_num_clusters):
        gmm = CustomGaussianMixture(n_components=n_clusters, covariance_type='full', n_init = 5, dim_emph = dim_emph)
        gmm.fit(clustering_input)
        cur_bic = gmm.bic(clustering_input)
        if cur_bic < best_bic:
            best_gmm = gmm
            best_bic = cur_bic

    return best_gmm, best_gmm.predict(clustering_input)


def _gmm_confidence_check(X, prediction, clusterer, check_for_variance_dims, return_subclustering_recommendation = False):
    ''' Checks whether we can be sure about the assignment of an event.

    The following checks are performed:
    - 1. When the cluster has a too large StdDev
    - 2. When the cluster is too small checks whether the points X, Y lie in the
    - 3. If probability is lower than 80%
    - 4. If the event is higher than 80% but lies outside the 3 sigma confidence intervall of the gmm distribution.


    Parameters
    ----------
    X: pd.DataFrame
          The events to cluster
    prediction: pd.Series
        The applince of each element, it has been applied to.
    clusters: scikit.MixtureModel
        The clusterer responsible for the events
    check_for_variance_dims: list
        The given columns are considered to exclude a cluster because of too high variance
    return_subclustering_recommendation: bool
        When set to true, the result contain a hint, whether it is worth to subcluster a certain clustering.

    Returns
    -------
    confidence: np.array<bool>
        Values definde, whether the appliance is confident for a certain event
    subclustering: np.array<bool>
        For each cluster defines whether it is reasonable to create a subcluster.
    '''

    if (prediction == -1).all():
        if return_subclustering_recommendation:
            return np.zeros(len(X)).astype(bool), np.zeros(len(X)).astype(bool)
        else:
            return np.zeros(len(X)).astype(bool)

    subcluster_recommendations = []
    confident = np.zeros(X.shape[0]).astype(bool)
    unique, counts = np.unique(prediction, return_counts=True)
    avg_clustersize = np.mean(counts)
    counts = dict(zip(unique, counts))
    for i, (mean, covar) in enumerate(zip(clusterer.means_, clusterer.covariances_)):

        # 1. Exclude clusters with too high stddev
        indices = np.where(np.in1d(X.columns, check_for_variance_dims))[0]
        stddevs = np.sqrt(covar.diagonal()[indices])
        means =  np.abs(mean[indices])
        subcluster_recommendations.append(i)
        if ((stddevs > 0.3 * means) & (stddevs > 10)).any() and not ((stddevs < 0.01 * means).any()):
            continue

        # 2. Exclude too small clusters!
        if (not i in counts) or (counts[i] < 0.1 * avg_clustersize) or (counts[i] < 5):
            continue

        # Take points of current cluster
        cur = (prediction == i)
        cur_X = X[cur].values

        # 3. Check for confidence by the probability
        probas = clusterer.predict_proba(cur_X)
        confident[cur] = probas.max(axis=1) > 0.9

        # 4. If not 90% sure, take at least the ones inside the one sigma environment
        confident[cur] |= \
            (spatial.distance.cdist(cur_X, np.expand_dims(mean, 0), 'mahalanobis', VI=linalg.inv(covar)) < 1).squeeze()

    if return_subclustering_recommendation:
        return confident.astype(bool), subcluster_recommendations
    else:
        return confident.astype(bool)


def _gmm_clustering_hierarchical(events, name, max_num_clusters=[5,5], check_for_variance_dims = [[],[]], dim_scaling = [{},{}], dim_emph = [{},{}], single_only = True):
    '''
    This function does the clustering in a hierarchical way.
    That means, that too large clusters of the first step are again clustered.
    All paramters contain multiple two inputs. For the first and the second clustering step.

    Parameters
    ----------
    events: pd.DataFrame
        The events to cluster
    name: string
        The name of the cluster to build. Namely its size ("2", "3", "4")
    max_num_clusters: [int, int]
        The mount
    check_for_variance_dims: list
        The given columns are considered to exclude a cluster because of too high variance
    dim_scaling = [float, float]
        Scales a certain dimension in a preprocessing step. This does not include GMM but the results of
        the K-means which is used to find the starting values for Gmm
    dim_emph = [{},{}],
        Emphasizes a certain dimension of the clustering of appliances. This is done by scaling the covariance
        of the GMM's EM-algorthms.
    single_only: bool
        When set to true only a single step of clustering is performed. Deactivates hierarchical clustering.

    Returns
    -------
     clusterers: dict<str, scikit.GaussianMixture>
        Dictionary mapping from a subtype to a clusterer
     subtypes: np.array<str>
        A string specifying the clustering step.
     labels: np.array<int>
        The appliances a certain event has been clustered to.
     confidence: np.array<bool>
        Values definde, whether the appliance is confident for a certain event
    '''
    # If single value given, automatically create lists
    if not type(max_num_clusters) is list:
        max_num_clusters=[max_num_clusters, max_num_clusters]
    if not type(check_for_variance_dims) is list:
        check_for_variance_dims = [check_for_variance_dims,check_for_variance_dims]
    elif len(check_for_variance_dims) == 0 or not type(check_for_variance_dims[0]) is list:
        check_for_variance_dims = [check_for_variance_dims,check_for_variance_dims]
    if not type(dim_scaling) is list:
        dim_scaling = [dim_scaling, dim_scaling]
    if not type(dim_emph) is list:
        dim_emph = [dim_emph ,dim_emph]

    # First round of clustering
    clusterer, labels = _gmm_clustering(events, max_num_clusters=max_num_clusters[0], dim_scaling=dim_scaling[0], dim_emph=dim_emph[0])
    subtypes = np.empty(shape =(len(events),),  dtype=object)
    subtypes [:] = name
    clusterers = {name: clusterer}

    # Check confidence
    confidence, subclustering = \
        _gmm_confidence_check(events, labels, clusterer, check_for_variance_dims=check_for_variance_dims[0],
                              return_subclustering_recommendation=True)

    # Second round of clustering if not removed
    if not single_only:
        for mastercluster in subclustering:
            sub_name = name + "_" + str(mastercluster)
            cur_cluster = (labels == mastercluster)
            to_subcluster = events[cur_cluster]
            sub_clusterer, sub_labels = \
                _gmm_clustering(to_subcluster, max_num_clusters=max_num_clusters[1],
                                dim_scaling=dim_scaling[1], dim_emph=dim_emph[1])
            sub_confidence = \
                _gmm_confidence_check(to_subcluster, sub_labels, sub_clusterer,
                                      check_for_variance_dims=check_for_variance_dims[1])
            labels[cur_cluster] = sub_labels
            confidence[cur_cluster] = sub_confidence
            subtypes[cur_cluster] = sub_name
            clusterers[sub_name] = sub_clusterer

    # Return the overall results (distinguish by subtype)
    return clusterers, subtypes, labels, confidence


def _gmm_hierarchical_predict_and_confidence_check(name, events, clusterers, check_for_variance_dims=[]):
    '''
    This function does the prediction and the subsequent confidence check.
    Used for the twoevents inside the fourevents.

    Parameters
    ----------
    name: string
        The name of the cluster to build. Namely its size ("2", "3", "4")
    events: pd.DataFrame
        The events to cluster
    clusterers: dict<str, scikit.GaussianMixture>
        Dictionary mapping from a subtype to a clusterer
    check_for_variance_dims: list
        The given columns are considered to exclude a cluster because of too high variance

    Returns
    -------
     labels: np.array<int>
        The appliances a certain event has been clustered to.
     subtypes: np.array<str>
        A string specifying the clustering step.
     confidence: np.array<bool>
        Values definde, whether the appliance is confident for a certain event
    '''

    # Find the relevant clusterer
    main_clusterer = clusterers[name]
    sub_clusterers = []
    for k in clusterers.keys():
        if k.startswith(name + "_"):
            sub_clusterers.append(k)

    # Do a prediction and subpredict if subclusterer available
    labels = main_clusterer.predict(events)
    confident = -np.ones(len(events))
    subtypes = np.empty(shape =(len(events),),  dtype=object)
    for sub_clusterer in sub_clusterers:
        cur = labels == int(sub_clusterer[2:])
        cur_events = events[cur]
        if cur_events.empty:
            continue
        labels[cur] = clusterers[sub_clusterer].predict(cur_events)
        confident[cur] = _gmm_confidence_check(cur_events, labels[cur], clusterers[sub_clusterer], check_for_variance_dims=check_for_variance_dims).astype(int)
        subtypes[cur] = sub_clusterer

    # Also check confidence for events, which do not belong to subcluster
    cur = confident == -1
    cur_events = events[cur]
    confident[cur] = _gmm_confidence_check(cur_events, labels[cur], main_clusterer, check_for_variance_dims=check_for_variance_dims).astype(int)
    subtypes[cur] = name
    confident = confident.astype(bool)
    return labels, confident, subtypes




class EventbasedCombination(UnsupervisedDisaggregator):
    """ This disaggregator is a combination of available event-based disaggregators.
    First fitting flags are combined and the created events are clustered the like Hart does it.
    Then the longer segments are created and observed by a clustering of the flags and a subsequent
    combination into events. This order has been proposed by Baranski.

    Results are stored in two ways. This is different from the other disaggregating classes.
    -   The usual disaggregated store is stored with a resolution of 5 minutes. This is enough to
        do further steps like forecasting.
    -   To evaluate NILM it is also possible to store exact results. In this case, the
        additional file only stores the flanks. During read all other values have to be
        reconstructed by using interpolate. That reduces load times significantly.
    """

    """ Necessary attributes of any approached meter """
    Requirements = {
        'max_sample_period': 10,
        'physical_quantities': [['power','active']]
    }

    """ The relate my model """
    model_class = EventbasedCombinationDisaggregatorModel


    def __init__(self, model = None):
        if model == None:
            model = self.model_class();
        self.model = model;
        super(EventbasedCombination, self).__init__()



    def disaggregate(self, metergroup, output_datastore, exact_nilm_datastore = None, tmp_folder = None, plotting_path = False, **kwargs):
        """ Trains and immediatly disaggregates

        Parameters
        ----------
        metergroup: nilmtk.MeterGroup
            The metergroup of the buildings main meters.
            Can also be a list of [transients, steady_states] In that case, the given data is 
            immediately used and the loading step is skipped.
        output_datastore: nilmtk.Datastore
            Storage where the disaggregated meters are stored
        exact_nilm_datastore: nilmtk.Datastore
            If set, the NILM Output is also stored as an exact result with full resolution.
        tmp_folder: str
            Path to a folder where intermediate results shall be stored.
            It let none, no intermediate results are stored
        plotting_path: str or False
            If set, a folder where plots are stored
        """
        if not tmp_folder is None and not tmp_folder.endswith("/"):
            tmp_folder = tmp_folder + "/"

        ### Prepare
        #pool = Pool(processes=3)
        model = self.model
        model.steady_states = []
        model.transients = []
        model.appliances = []
        model.appliances_detailed = []
        model.clusterer = [{} for _ in range(len(metergroup))]
        model.overall_powerflow = overall_powerflow = []
        

        # 1. Load the events from the powerflow data
        if type(metergroup) is list:
            model.transients = [metergroup[0]]
            model.steady_states = [metergroup[1]]
            model.overall_powerflow = [my_resample_highdef(model.transients[0]['active transition'], model.steady_states[0].index[0], model.steady_states[0].index[-1], '5min', ['active transition'])]
            num_phases = 1
            building_number = 1
            building_meta = {}
        else:
            kwargs = self._pre_disaggregation_checks(metergroup, kwargs)
            kwargs.setdefault('sections', metergroup.good_sections().merge_shorter_gaps_than('10min'))
            metergroup = metergroup.sitemeters()
            num_phases = len(metergroup)
            building_number = metergroup.building()
            building_meta = metergroup.meters[0].building_metadata

            print('Extract events')
            t1 = time.time()
            loader, steady_states_list, transients_list = [], [], []
            try:
              pass
              self.model = model = pckl.load(open(tmp_folder + str(metergroup.identifier) + '.pckl', 'rb'))
              model.appliances = []
            except:
                print("Load new")
                for i in range(num_phases):
                    overall_powerflow.append(None)
                    steady_states_list.append([])
                    transients_list.append([])
                    loader.append(metergroup.meters[i].load(cols=self.model.params['cols'], chunksize = 31000000, **kwargs))
                try:
                    while(True):
                        print("Loaded one more")
                        input_params = []
                        states_and_transients = []
                        for i in range(num_phases):
                            print("Loaded " + str(i))
                            power_dataframe = next(loader[i]).dropna()
                            if overall_powerflow[i] is None:
                                overall_powerflow[i] = power_dataframe.resample('5min').agg('mean')
                            else:
                                overall_powerflow[i] = \
                                    overall_powerflow[i].append(power_dataframe.resample('5min', how='mean'))
                            indices = np.array(power_dataframe.index)
                            values = np.array(power_dataframe.iloc[:,0])
                            input_params.append((indices, values, model.params['min_n_samples'],
                                                model.params['state_threshold'], model.params['noise_level']))
                            states_and_transients.append(find_transients_fast(input_params[-1]))
                        #states_and_transients = pool.map(find_transients_fast, input_params)
                        for i in range(len(metergroup)):
                            steady_states_list[i].append(states_and_transients[i][0])
                            transients_list[i].append(states_and_transients[i][1])
                except StopIteration:
                    pass
                # set model (timezone is lost within c programming)
                for i in range(num_phases):
                    model.steady_states.append(pd.concat(steady_states_list[i]).tz_localize('utc'))
                    model.transients.append(pd.concat(transients_list[i]).tz_localize('utc'))
                    model.transients[-1].index.rename("starts", inplace = True)
                if not tmp_folder is None:
                    pckl.dump(model, open(tmp_folder + str(metergroup.identifier) + '.pckl', 'wb'))
            print("Eventloading: " + str(time.time()-t1))

        #for i in range(len(model.transients)):
        #    model.transients[i] = model.transients[i]
        #    model.steady_states[i] = model.steady_states[i]
        #    model.overall_powerflow[i] = model.overall_powerflow[i]

        # 2. Create separate powerflows with events, shared by multiple phases
        t1 = time.time()
        try:
            #pass
            self.model = model = pckl.load(open(tmp_folder + str(metergroup.identifier) + '_phases_separated.pckl', 'rb'))
        except:
            if len(model.transients) > 1:
                model.transients, model.steady_states = \
                    separate_simultaneous_events(model.transients, model.steady_states, model.params['noise_level'])
            if not tmp_folder is None:
                pckl.dump(model, open(tmp_folder + str(metergroup.identifier) + '_phases_separated.pckl', 'wb'))
        print('Shared phase separation: ' + str(time.time() - t1))
        
        ## 3. Separate segments between base load
        try:
            self.model = model = pckl.load(open(tmp_folder + str(metergroup.identifier) + '_appfound.pckl', 'rb'))
        except:
            t1 = time.time()
            input_params = []
            for i in range(num_phases):
               input_params.append((self.model.transients[i], self.model.steady_states[i],
                                    self.model.params['state_threshold'], self.model.params['noise_level'], self.model.params['binary_spikes']))
               self.model.transients[i] = add_segments(input_params[-1])
            #self.model.transients = pool.map(add_segments, input_params)
            if plotting_path:
                plt_phase = 1 if num_phases > 0 else 0
                nilmtk.plots.latexify(fontsize=11)
                self.model.steady_states[plt_phase]['starts'] = self.model.transients[0]['starts']
                start = self.model.transients[plt_phase]['starts'][0].round('d')
                ax = self.model.transients[plt_phase].set_index("starts")['active transition'].loc[start + pd.Timedelta('1.25d'):start + pd.Timedelta('1.5d')].cumsum().resample('2s', how='ffill').plot()
                ax.set_ylabel("Power [W]")
                ax.set_xlabel("Time")
                plt.savefig(plotting_path + "\original_powerflow.pdf")
                
                nilmtk.plots.latexify(fontsize=11)
                nilmtk.plots.plot_segments(self.model.transients[plt_phase].set_index('starts')[start + pd.Timedelta('1.25d'):start + pd.Timedelta('1.5d')].reset_index(), 
                                            self.model.steady_states[plt_phase][start + pd.Timedelta('1.25d'):start + pd.Timedelta('1.5d')])
                plt.savefig(plotting_path + "\segmentation_segments.pdf")
            print('Segment separation: ' + str(time.time() - t1))

            ## 4. Create all events which per definition have to belong together (tuned Hart)
            t2 = time.time()
            result = []
            input_params = []
            for i in range(num_phases):
               input_params.append((model.transients[i], self.model.params['state_threshold'], plotting_path))
               result.append(find_appliances(input_params[-1]))
            #result = pool.map(find_appliances, input_params)
            for i in range(num_phases):
               model.transients[i] = result[i]['transients']
               model.clusterer[i] = result[i]['clusterer']
            if not tmp_folder is None:
               pckl.dump(model, open(tmp_folder + str(metergroup.identifier) + '_appfound.pckl', 'wb'))
            print("Find appliances: " + str(time.time() - t2))
        print("Found appliances")
        
        # 5. Create the appliances (Pay attention, id per size and subtype) and rest powerflow
        try:
            self.model = model = pckl.load(open(tmp_folder + str(metergroup.identifier) + '_appcreated.pckl', 'rb'))
        except:
            model.appliances_detailed = []
            t3 = time.time()
            input_params, results = [], []
            for i in range(num_phases): #len(model.transients)):
                input_params.append((self.model.transients[i], self.model.overall_powerflow[i], self.model.params['min_appearance'], not exact_nilm_datastore is None))
                results.append(create_appliances(input_params[-1]))
            #results = pool.map(create_appliances, input_params)
            for i in range(num_phases):
               model.appliances.append(results[i]['appliances'])
               model.overall_powerflow[i] = results[i]['overall_powerflow']
               if exact_nilm_datastore:
                   model.appliances_detailed.append(results[i]['exact_nilm'])
            # Then create the multiphase events (has to be done, one after another)
            for i in range(num_phases, len(model.transients)):
                result = create_multiphase_appliances((self.model.transients[i], self.model.overall_powerflow, not exact_nilm_datastore is None))
                model.appliances.append(result['appliances'])
                model.overall_powerflow = result['overall_powerflow']
                if exact_nilm_datastore:
                   model.appliances_detailed.append(result['exact_nilm'])
            if not tmp_folder is None:
               pckl.dump(model, open(tmp_folder + str(metergroup.identifier) + '_appcreated.pckl', 'wb'))
            print("Put together appliance powerflows: " + str(time.time() - t3))
        print("Appliance powerflows put together.")


        # 5. Store the results (Not in parallel since writing to same file)
        print('Store')
        t4 = time.time()
        for phase in range(len(model.transients)):
            building_path = '/building{}'.format(building_number * 10 + phase)
            for i in range(len(self.model.appliances[phase])):
                key = '{}/elec/meter{:d}'.format(building_path, i + 2) # 0 not existing and Meter1 is rest
                output_datastore.append(key, self.model.appliances[phase][i]) 
                if not exact_nilm_datastore is None:
                    exact_nilm_datastore.append(key, self.model.appliances_detailed[phase][i])
            output_datastore.append('{}/elec/meter{:d}'.format(building_path, 1), self.model.overall_powerflow[phase])
        num_meters = [len(cur) + 1 for cur in self.model.appliances] 
        stores = [(output_datastore, 300, True)] if exact_nilm_datastore is None else [(output_datastore, 300, True), (exact_nilm_datastore, 0, False)]
        for store, res, rest_included in stores:
            self._save_metadata_for_disaggregation(
                output_datastore = store,
                sample_period = res, 
                measurement=self.model.overall_powerflow[0].columns,
                timeframes=list(kwargs['sections']) if 'sections' in kwargs else TimeFrameGroup([TimeFrame(start=model.transients[0].index[0],end=model.transients[0].index[-1])]),
                building=building_number,
                supervised=False,
                num_meters=num_meters,
                original_building_meta= building_meta,
                rest_powerflow_included = rest_included
            )
        print("STORED: " + str(time.time()-t4) + "\n\n")



    def disaggregate_single_phase(self, transients, steady_states):
        '''
        This function does the disaggregation of a single phase. 
        It has been mainly created as a test function for testing the 
        synthetic data.

        Parameters
        ----------
        transients: pd.DataFrame
            The steady states of the powerflow as disaggregated from the detected from
            the powerflow.
        steady_states: pd.DataFrame
            The steady states of the powerflow as disaggregated from the detected from
            the powerflow.

        Returns
        -------
        appliances: [pd.DataFrame,...]
            The succesfully disaggreagated appliances
        rest: pd.DataFrame
            The remaining powerflow which could not be assigned to the appliances.
        '''
        # Separate segments
        t1 = time.time()
        transients = add_segments([transients, steady_states, self.model.params['state_threshold'], self.model.params['noise_level'], self.model.params['binary_spikes']])
        print('Segment separation: ' + str(time.time() - t1))

        # Create all events which per definition have to belong together
        t2 = time.time()
        tmp = find_appliances([transients, self.model.params['state_threshold']])
        transients = tmp['transients']
        clusterer = tmp['clusterer']
        print("Find appliances: " + str(time.time() - t2))

        # Create the appliances
        t3 = time.time()
        input_params, results = [], []
        rest_powerflow = my_resample_highdef(steady_states['active transition'], steady_states.index[0], steady_states.index[-1], '5min', ['active transition'])
        tmp = create_appliances([transients, rest_powerflow, self.model.params['min_appearance'], True])
        print("Put together appliance powerflows: " + str(time.time() - t3))
        return tmp
   



    #region Clustering steps

    def _cluster_segments(self, cluster_df, method = 'kmeans'):
        '''
        Does the clustering in two steps to find everything
        '''

        if len(cluster_df)-1 <= len(cluster_df.columns):
            return pd.DataFrame(columns=cluster_df.columns), (-1)*np.ones(len(cluster_df)), []


        # First clustering
        X = cluster_df.values.reshape((len(cluster_df.index), len(cluster_df.columns)))
        clusterer = KMeans(n_jobs = 2)
        clusterer.fit(X)
        clusterer1 = MeanShift(cluster_all = False, min_bin_freq = 20, n_jobs = 1)
        clusterer1.fit(X)
        labels = clusterer1.labels_
        cluster_centers = clusterer1.cluster_centers_
        clusterer = [clusterer1]

        # Do a second round if necessary
        rest_idx = labels == -1
        if rest_idx.sum() > len(cluster_df.columns)+1:
            clusterer2 = MeanShift(cluster_all = False, min_bin_freq = 20, n_jobs = 1)
            clusterer2.fit(cluster_df[rest_idx])
            labels_rest = clusterer2.labels_
            centers_rest = clusterer2.cluster_centers_
            labels_rest[labels_rest != -1] = labels_rest[labels_rest != -1] + len(cluster_centers)
            labels[rest_idx] = labels_rest
            cluster_centers = np.append(cluster_centers, centers_rest, axis=0)
            clusterer.append(clusterer2)

        return pd.DataFrame(cluster_centers, columns=cluster_df.columns), labels, clusterer


    
    def _check_for_membership(self, cluster_df, clusterers, clusters, cluster_limits):
        '''
        The
        '''
        if len(cluster_df) == 0:
            return pd.DataFrame(columns=['label'])

        # Prepare for checking
        original_columns = cluster_df.columns
        X = cluster_df.values.reshape((len(cluster_df.index), len(cluster_df.columns)))
        cluster_df['label'] = np.nan
        labels = []
        prevlabels = 0
        for clusterer in clusterers:
            # Find appliance
            curlabels = clusterer.predict(X)
            curlabels[curlabels != -1] = curlabels[curlabels != -1] + prevlabels
            prevlabels += len(clusterer.cluster_centers_)

            # Check validity
            cluster_df['tmp_label'] = curlabels 
            cluster_df = cluster_df.join(cluster_limits, on='tmp_label', rsuffix="_limit")
            cluster_df = cluster_df.join(clusters, on='tmp_label', rsuffix="_center")
            valid = np.ones(len(cluster_df), dtype=bool)
            for original_column in original_columns:
                delta = cluster_df[original_column] - cluster_df[original_column + '_center']
                valid &= delta < cluster_df[original_column + '_maxdelta']
            
            cluster_df.update(cluster_df[['tmp_label']][valid].rename(columns={'tmp_label':'label'}))
            #pd.DataFrame({'label':((cluster_df['tmp_label'] * (valid.astype(int)*2-1)) -1 + valid.astype(int)).clip(-1)}))
            
        return cluster_df[['label']]
            

    def _cluster_events(self, events, max_num_clusters=5, exact_num_clusters=None):
        ''' Applies clustering on the previously extracted events. 
        The _transform_data function can be removed as we are immediatly passing in the 
        pandas dataframe.

        Parameters
        ----------
        events : pd.DataFrame with the columns "PowerDelta, Duration, MaxSlope"
        max_num_clusters : int
        exact_num_clusters: int
        method: string Possible approaches are "kmeans" and "ward"
        Returns
        -------
        centroids : ndarray of int32s
            Power in different states of an appliance, sorted
            
        labels: ndarray of int32s
            The assignment of each event to the events
        '''
        
        # Preprocess dataframe
        mapper = DataFrameMapper([(column, None) for column in events.columns])
        clustering_input = mapper.fit_transform(events.copy())
    
        # Do the clustering
        best = -1
        labels = {}
        cluster_centers = {}
        labels_unique = {}
        clusterer = {}
        score = {}

        # If the exact number of clusters are specified, then use that
        if exact_num_clusters is not None:
            labels, centers = _apply_clustering_n_clusters(clustering_nput, exact_num_clusters, method)
            return centers.flatten()

        # Special case:
        if len(events) == 1: 
            return np.array([events.iloc[0]["active transition"]]), np.array([0])

        # If exact cluster number not specified, use cluster validity measures to find optimal number
        for n_clusters in range(3, max_num_clusters): #ACHTUNG AUF 3 gestellt
            try:
                # Do a clustering for each amount of clusters
                labels, centers, clusterer, score = self._apply_clustering_n_clusters(clustering_nput, n_clusters, method, score = True)
                labels[n_clusters] = labels
                cluster_centers[n_clusters] = centers
                labels_unique[n_clusters] = np.unique(labels)
                clusterer[n_clusters] = clusterer
                score[n_clusters] = score
                if score < score[best]:
                    best = n_clusters

            except Exception:
                if num_clusters > -1:
                    return cluster_centers[num_clusters]
                else:
                    return np.array([0])

        return pd.DataFrame(cluster_centers[num_clusters], columns=events.columns), labels[num_clusters], clusterer[num_clusters]
    
        # Postprocess and return clusters (weiss noch nicht ob das notwendig ist)
        centroids = np.append(centroids, 0)  # add 'off' state
        centroids = np.round(centroids).astype(np.int32)
        centroids = np.unique(centroids)  # np.unique also sorts
        return centroids
    


    def _apply_clustering_n_clusters(self, X, n_clusters, method='kmeans'):
        """
        :param X: ndarray
        :param n_clusters: exact number of clusters to use
        :param method: string kmeans or ward
        :return:
        """
        if method == 'kmeans':
            k_means = KMeans(init='k-means++', n_clusters=n_clusters)
            k_means.fit(X)
            sh_n = silhouette_score(events, labels[n_clusters], metric='euclidean', sample_size = min(len(labels[n_clusters]),5000))
            return k_means.labels_, k_means.cluster_centers_,k_means,shn
        elif method == 'gmm':
            gmm = mixture.GaussianMixture(n_components=n_clusters, covariance_type='full')
            gmm.fit(X)
            
            try:
                sh_n = silhouette_score(events, labels[n_clusters], metric='euclidean', sample_size = min(len(labels[n_clusters]),5000))
                if sh_n > silhouette:
                    silhouette = sh_n
                    num_clusters = n_clusters
            except Exception as inst:
                num_clusters = n_clusters
            return (gmm.means_, gmm.covariances_), gmm, gmm.bic
    
    #endregion




    #region So far unsused 
    

    #def disaggregate_chunk(self, chunk, prev, transients, phase):
    #    """
    #    Parameters
    #    ----------
    #    chunk : pd.DataFrame
    #        mains power
    #    prev
    #    transients : returned by find_steady_state_transients

    #    Returns
    #    -------
    #    states : pd.DataFrame
    #        with same index as `chunk`.
    #    """
    #    model = self.model

    #    load_kwargs = self._pre_disaggregation_checks(mains, load_kwargs)
    #    load_kwargs.setdefault('sample_period', 60)
    #    load_kwargs.setdefault('sections', mains.good_sections())

    #    states = pd.DataFrame(
    #        np.NaN, index=chunk.index, columns= model.centroids[phase].index.values)
    #    for transient_tuple in transients.itertuples():
            
    #        # Transient in chunk
    #        if chunk.index[0] < transient_tuple[0] < chunk.index[-1]:
    #            # Absolute value of transient
    #            abs_value = np.abs(transient_tuple[1:])
    #            positive = transient_tuple[1] > 0
    #            abs_value_transient_minus_centroid = pd.DataFrame(
    #                (model.centroids[phase] - abs_value).abs())
    #            if len(transient_tuple) == 2:
    #                # 1d data
    #                index_least_delta = abs_value_transient_minus_centroid.idxmin().values[0]
    #                value_delta = abs_value_transient_minus_centroid.iloc[index_least_delta].values[0]
    #                # Only accept it if it really fits exactly
    #                tolerance = self.calc_tolerance(abs_value[0], model.centroids[phase].iloc[index_least_delta].values[0]) 
    #                if value_delta > tolerance:
    #                    continue

    #                # ALSO HIER MUSS ICH SCHAUEN OB ES DURCHLAEUFT. Und sonst koennte ich auch direkt vom Trainieren die Disaggregation nehmen

    #            else:
    #                # 2d data.
    #                # Need to find absolute value before computing minimum
    #                columns = abs_value_transient_minus_centroid.columns
    #                abs_value_transient_minus_centroid["multidim"] = (
    #                    abs_value_transient_minus_centroid[columns[0]] ** 2
    #                    +
    #                    abs_value_transient_minus_centroid[columns[1]] ** 2)
    #                index_least_delta = (
    #                    abs_value_transient_minus_centroid["multidim"].argmin())
    #            if positive:
    #                # Turned on
    #                states.loc[transient_tuple[0]][index_least_delta] = model.centroids[phase].ix[index_least_delta].values
    #            else:
    #                # Turned off
    #                states.loc[transient_tuple[0]][index_least_delta] = 0
    #    #prev = states.iloc[-1].to_dict()
    #    states['rest'] = chunk - states.ffill().fillna(0).sum(axis=1)
    #    return states.dropna(how='all') #pd.DataFrame(states, index=chunk.index)


    #def translate_switches_to_power(self, states_chunk):
    #    '''
    #    This function translates the states into power. The intermedite 
    #    steps are not reconstructed, as this does not have to be stored.
    #    It will be faster to do this manually after loading.
    #    '''
    #    model = self.model
    #    di = {}
    #    ndim = len(model.centroids[phase].columns)
    #    for appliance in states_chunk.columns:
    #        states_chunk[[appliance]][ states_chunk[[appliance]]==1] = model.centroids[phase].ix[appliance].values
    #    return states_chunk


    #def assign_power_from_states(self, states_chunk, prev, phase):

    #    di = {}
    #    ndim = len(self.model.centroids[phase].columns)
    #    for appliance in states_chunk.columns:
    #        values = states_chunk[[appliance]].values.flatten()
    #        if ndim == 1:
    #            power = np.zeros(len(values), dtype=int)
    #        else:
    #            power = np.zeros((len(values), 2), dtype=int)
    #        # on = False
    #        i = 0
    #        while i < len(values) - 1:
    #            if values[i] == 1:
    #                # print("A", values[i], i)
    #                on = True
    #                i = i + 1
    #                power[i] = self.model.centroids[phase].ix[appliance].values
    #                while values[i] != 0 and i < len(values) - 1:
    #                    # print("B", values[i], i)
    #                    power[i] = self.model.centroids[phase].ix[appliance].values
    #                    i = i + 1
    #            elif values[i] == 0:
    #                # print("C", values[i], i)
    #                on = False
    #                i = i + 1
    #                power[i] = 0
    #                while values[i] != 1 and i < len(values) - 1:
    #                    # print("D", values[i], i)
    #                    if ndim == 1:
    #                        power[i] = 0
    #                    else:
    #                        power[i] = [0, 0]
    #                    i = i + 1
    #            else:
    #                # print("E", values[i], i)
    #                # Unknown state. If previously we know about this
    #                # appliance's state, we can
    #                # use that. Else, it defaults to 0
    #                if prev[appliance] == -1 or prev[appliance] == 0:
    #                    # print("F", values[i], i)
    #                    on = False
    #                    power[i] = 0
    #                    while values[i] != 1 and i < len(values) - 1:
    #                        # print("G", values[i], i)
    #                        if ndim == 1:
    #                            power[i] = 0
    #                        else:
    #                            power[i] = [0, 0]
    #                        i = i + 1
    #                else:
    #                    # print("H", values[i], i)
    #                    on = True
    #                    power[i] = self.model.centroids[phase].ix[appliance].values
    #                    while values[i] != 0 and i < len(values) - 1:
    #                        # print("I", values[i], i)
    #                        power[i] = self.model.centroids[phase].ix[appliance].values
    #                        i = i + 1

    #        di[appliance] = power
    #        # print(power.sum())
    #    return di


    #def disaggregate(self, mains, output_datastore = None, **load_kwargs):
    #    """Disaggregate mains according to the model learnt previously.

    #    Parameters
    #    ----------
    #    mains : nilmtk.ElecMeter or nilmtk.MeterGroup
    #    output_datastore : instance of nilmtk.DataStore subclass
    #        For storing power predictions from disaggregation algorithm.
    #    sample_period : number, optional
    #        The desired sample period in seconds.
    #    **load_kwargs : key word arguments
    #        Passed to `mains.power_series(**kwargs)`
    #    """
    #    model = self.model

    #    load_kwargs = self._pre_disaggregation_checks(mains, load_kwargs)
    #    load_kwargs.setdefault('sample_period', 60)
    #    load_kwargs.setdefault('sections', mains.good_sections())

    #    timeframes = []
    #    #building_path = '/building{}'.format(mains.building())
    #    #mains_data_location = building_path + '/elec/meter' + phase
    #    data_is_available = False

    #    transients = []
    #    steady_states = []
    #    for i in range(len(mains)):
    #        s, t = find_steady_states_transients_fast(
    #            mains.meters[i], model.params['cols'], noise_level=model.params['noise_level'], state_threshold= model.params['state_threshold'], **load_kwargs)
    #        steady_states.append(s.tz_localize('utc'))
    #        transients.append(t.tz_localize('utc'))

    #    print('Separate Multiphase Segments')
    #    transients = self.separate_simultaneous_events(transients)

    #    # Initially all appliances/meters are in unknown state (denoted by -1)
    #    prev = [OrderedDict()] * len(transients)
        
    #    timeframes = []
    #    disaggregation_overall = [None] * len(transients)
    #    for phase in range(len(transients)):
    #        building_path = '/building{}'.format(mains.building()* 10 + phase)
    #        mains_data_location = building_path + '/elec/meter1'

    #        learnt_meters = model.centroids[phase].index.values
    #        for meter in learnt_meters:
    #            prev[phase][meter] = -1

    #        # Now iterating over mains data and disaggregating chunk by chunk
    #        to_load = mains.meters[i] if i < 3 else mains
    #        first = True
    #        for chunk in to_load.power_series(**load_kwargs):  # HIER MUSS ICH DOCH UEBER DIE EINZELNEN PHASEN LAUFEN!!!
    #            # Record metadata
    #            if phase == 1: # Only add once
    #                timeframes.append(chunk.timeframe)
    #                measurement = chunk.name # gehe davon aus, dass ueberall gleich
    #            power_df = self.disaggregate_chunk(
    #                chunk, prev[phase], transients[phase], phase) # HAT DEN VORTEIL, DASS ICH MICH AUF DIE GUTEN SECTIONS KONZENTRIEREN KANN
    #            if first:
    #                #power_df = pd.DataFrame(0, columns=power_df.columns, index = chunk.index[:1]).append(power_df) # Ich gehe davon aus, dass alle werte ankommen
    #                power_df.iloc[0, :] = 0 
    #                first = False

    #            cols = pd.MultiIndex.from_tuples([chunk.name])

    #            if output_datastore != None:
    #                for meter in learnt_meters:
    #                    data_is_available = True
    #                    df = power_df[[meter]].dropna()
    #                    df.columns = cols
    #                    key = '{}/elec/meter{:d}'.format(building_path, meter + 2) # Weil 0 nicht gibt und Meter1 das undiaggregierte ist und 
    #                    output_datastore.append(key, df)
    #                df = power_df[['rest']].dropna()
    #                df.columns = cols
    #                output_datastore.append(key=mains_data_location, value= df) #pd.DataFrame(chunk, columns=cols))  # Das Main wird auf Meter 1 gesetzt.
    #            else:
    #                if disaggregation_overall is None:
    #                    disaggregation_overall = power_df
    #                else:
    #                    disaggregation_overall = disaggregation_overall.append(power_df)
    #    if output_datastore != None:
    #        # Write a very last entry. Then at least start and end set
    #        df = pd.DataFrame(0., columns = cols, index = chunk.index[-1:])
    #        for phase in range(len(transients)):
    #            building_path = '/building{}'.format(mains.building()* 10 + phase)
    #            for meter in model.centroids[phase].index.values:
    #                key = '{}/elec/meter{:d}'.format(building_path, meter + 2) # Weil 0 nicht gibt und Meter1 das undiaggregierte ist und 
    #                output_datastore.append(key, df)
    #        # Hier muss ich den rest noch setzen noch setzen
    #        #output_datastore.append(key, df.rename(columns={0:meter})) Ich gehe davon aus, dass alle Daten rein kommen und ich rest nicht setzen muss

    #        # Then store the metadata
    #        num_meters = [len(cur) + 1 for cur in self.model.centroids] # Add one for the rest
    #        if data_is_available:
    #            self._save_metadata_for_disaggregation(
    #                output_datastore=output_datastore,
    #                sample_period=load_kwargs['sample_period'],
    #                measurement=col,
    #                timeframes=timeframes,
    #                building=mains.building(),
    #                supervised=False,
    #                num_meters = num_meters,
    #                original_building_meta = mains.meters[0].building_metadata
    #            )
    #    else:
    #        return disaggregation_overall





        # Der NEUE ANSATZ, den ich mir jetzt dovh verkneife
        #    for chunk in mains.meters[phase].power_series(**load_kwargs):
        #        # Record metadata
        #        timeframes[phase].append(chunk.timeframe)
        #        measurement = chunk.name
        #        power_df = self.disaggregate_chunk(
        #            chunk, prev[phase], transients[phase], phase)

        #        cols = pd.MultiIndex.from_tuples([chunk.name])
                
        #        # Die Meter muss ich noch durchiterieren
        #        id = str(datetime.now()).replace(" ", "").replace('-', '').replace(":","")
        #        id = id[:id.find('.')]
        #        keytemplate = '/building{0}/elec/disag/eventbased{1}/meter{2}'
        #        if output_datastore != None:
        #            for meter in learnt_meters:
        #                data_is_available = True
        #                df = power_df[[meter]].dropna() # remove the remaining nans
        #                df.columns = cols
        #                key = keytemplate.format(mains.building(), id, phase) + "/appliance{:d}".format(meter + 2) # Weil 0 nicht gibt und Meter1 der remaining powerflow ist
        #                output_datastore.append(key, df)
         
        #            # Store the remaining powerflow (the power not assigned to any facility) 
        #            df = power_df['rest'].dropna() # remove the remaining nans
        #            df.columns = cols
        #            key = keytemplate.format(mains.building(), id, phase) + "/appliance1"
        #            output_datastore.append(key, df)

        #            # Store the metadata
        #            if data_is_available:
        #                self._save_metadata_for_disaggregation(
        #                    output_datastore=output_datastore,
        #                    key = keytemplate.format(mains.building(), id, phase)
        #                )
                    
        #            #output_datastore.append(key=mains_data_location,
        #            #                    value=pd.DataFrame(chunk, columns=cols))  # Wir sparen uns den Main noch mal neu zu speichern
        #        else:
        #            if disaggregation_overall[phase] is None:
        #                disaggregation_overall[phase] = power_df
        #            else:
        #                disaggregation_overall[phase] = disaggregation_overall.append(power_df)

        #if output_datastore == None:
        #    return disaggregation_overall


    """
    def export_model(self, filename):
        model_copy = {}
        for appliance, appliance_states in self.model.iteritems():
            model_copy[
                "{}_{}".format(appliance.name, appliance.instance)] = appliance_states
        j = json.dumps(model_copy)
        with open(filename, 'w+') as f:
            f.write(j)

    def import_model(self, filename):
        with open(filename, 'r') as f:
            temp = json.loads(f.read())
        for appliance, centroids in temp.iteritems():
            appliance_name = appliance.split("_")[0].encode("ascii")
            appliance_instance = int(appliance.split("_")[1])
            appliance_name_instance = ApplianceID(
                appliance_name, appliance_instance)
            self.model[appliance_name_instance] = centroids
    """
