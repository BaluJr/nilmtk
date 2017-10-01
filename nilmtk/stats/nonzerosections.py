from __future__ import print_function, division
import numpy as np
from numpy import diff, concatenate
import gc
from .nonzerosectionsresults import NonZeroSectionsResults
from ..timeframe import TimeFrame
from ..utils import timedelta64_to_secs
from ..node import Node
from ..timeframe import list_of_timeframes_from_list_of_dicts, timeframe_from_dict


class NonZeroSections(Node):
    """Locate sections of data where the samples are bigger 
    larger 0. This is mostly used for disaggregated powerflows
    where there is really a power of 0 when the appliance 
    is not nonzero. Do not confuse it with the 'get_activations' 
    function of elecmeter. That function is not cached and returns 
    the real dataframe, while this stat only defines the borders.

    Only regards sections longer than 1 step. Because otherwise to many      !!!!!!!!! DAS MUSS ICH IMPLEMENTIEREN!!!!!!!!!!!
    problems.

    Attributes
    ----------
    previous_chunk_ended_with_open_ended_nonzero_section : bool
    """

    postconditions =  {'statistics': {'nonzero_sections': []}}
    results_class = NonZeroSectionsResults
        
    def reset(self):
        ''' nothing to do here '''
        pass

    def process(self):
        metadata = self.upstream.get_metadata()
        self.check_requirements()
        self.results = NonZeroSectionsResults()
        for chunk in self.upstream.process():
            self._process_chunk(chunk)
            yield chunk

    def _process_chunk(self, df):
        """
        Only checks where the chunk has nonzero values.

        Parameters
        ----------
        df : pd.DataFrame
            with attributes:
            - look_ahead : pd.DataFrame
            - timeframe : nilmtk.TimeFrame

        Returns
        -------
        None

        Notes
        -----
        Updates `self.results`
            Each nonzero section in `df` is marked with a TimeFrame.
            If this df ends with an open-ended nonzero section (assessed by
            examining df.look_ahead) then the last TimeFrame will have
            `end=None`. If this df starts with an open-ended nonzero section
            then the first TimeFrame will have `start=None`.
        """
        # Retrieve relevant metadata
        timeframe = df.timeframe

        # Process dataframe
        nonzero_sections = get_nonzero_sections(df)
 
        # Update self.results
        if nonzero_sections:
            self.results.append(timeframe, {'sections': [nonzero_sections]})




def _free_memory_dataframe(df):
    last_index = df[-1]
    del index
    gc.collect()
    return last_index

def get_nonzero_sections(df):
    """
    Parameters
    ----------
    df : pd.DataFrame

    Returns
    -------
    sections : list of TimeFrame objects
        Each nonzero section in `df` is marked with a TimeFrame.
        If this df ends with an open-ended nonzero section (assessed by
        examining `look_ahead`) then the last TimeFrame will have
        `end=None`.  If this df starts with an open-ended nonzero section
        then the first TimeFrame will have `start=None`.
    """

    # Find the switching actions, which stay constant for minimal_zerotime times
    minimal_zerotime = 10
    look_ahead = getattr(df, 'look_ahead', None)
    df = df > 0    

    tmp = df.astype(np.int).diff()
    nonzero_sect_starts = (tmp == 1)
    nonzero_sect_ends = (tmp == 0)
    for i in range(2,minimal_zerotime):
        tmp = df.astype(np.int).diff(i)
        nonzero_sect_starts *= tmp == 1
        nonzero_sect_ends *= tmp == 0
    tmp = df.astype(np.int).diff(minimal_zerotime)
    nonzero_sect_starts *=  tmp == 1
    nonzero_sect_ends *= tmp == -1
    del tmp
    nonzero_sect_starts = list(df[nonzero_sect_starts].dropna().index)
    nonzero_sect_ends   = list(df[nonzero_sect_ends.shift(-minimal_zerotime).fillna(False)].dropna().index)

    # If this chunk starts or ends with an open-ended
    # nonzero section then the relevant TimeFrame needs to have
    # a None as the start or end.
    for i in range(minimal_zerotime):
        if df.iloc[i, 0] == True:
            nonzero_sect_starts = [df.index[i]] + nonzero_sect_starts
            break

    if df.iloc[-1,0] == True:
        nonzero_sect_ends += [None]
    else:
        # Only start new zerosection when long enough, need look_ahead
        for i in range(1,minimal_zerotime+1):
            if df.iloc[-i, 0] != False:
                break

        if i < (minimal_zerotime):
            if look_ahead.head(minimal_zerotime-i).sum()[0] == 0:
                nonzero_sect_ends += [df.index[-i]] #, 0]]
            else:
                nonzero_sect_ends += [None]


    # Merge together ends and starts
    assert len(nonzero_sect_starts) == len(nonzero_sect_ends)
    sections = [TimeFrame(start, end)
                for start, end in zip(nonzero_sect_starts, nonzero_sect_ends)
                if not (start == end and start is not None)]

    # Memory management
    del nonzero_sect_starts
    del nonzero_sect_ends
    gc.collect()

    return sections
