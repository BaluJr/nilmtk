from __future__ import print_function, division
#from .accelerators_stat import get_nonzero_sections_fast
import numpy as np
from numpy import diff, concatenate
import gc
from .nonzerosectionsresults import NonZeroSectionsResults
from ..timeframe import TimeFrame
from ..utils import timedelta64_to_secs
from ..node import Node
from ..timeframe import list_of_timeframes_from_list_of_dicts, timeframe_from_dict
import pandas as pd

class NonZeroSections(Node):
    """ Locate sections of data where the load is larger than 0.
    
    This is mostly used for disaggregated powerflows
    where there is really a power of 0 when the appliance 
    is not nonzero. Do not confuse it with the 'get_activations' 
    function of elecmeter. That function is not cached and returns 
    the real dataframe, while this stat only defines the borders.

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
        self.results = NonZeroSectionsResults(2.3)
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
        nonzero_sections_starts, nonzero_sections_ends = get_nonzero_sections(df)

        # Update self.results
        #if nonzero_sections:
        self.results.append(timeframe, {'sections' : [{'start': nonzero_sections_starts, 'end': nonzero_sections_ends}]})


def get_nonzero_sections(df):
    """
    The input are always good_sections

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
    #minimal_zerotime = 3
    #look_ahead = getattr(df, 'look_ahead', None)
    df = df > 0    
    tmp = df.astype(np.int).diff()
    nonzero_sect_starts = (tmp == 1).values
    nonzero_sect_ends = (tmp == -1).values
    #nonzero_sect_starts = df[(tmp == 1).values].index
    if len(df) > 0:
        nonzero_sect_starts[0] = df.iloc[0,0] # = np.append(df.index[0], nonzero_sect_starts)
        nonzero_sect_ends[-1] |= df.iloc[-1,0] #np.append(nonzero_sect_ends, df.index[-1]) # FUCK THIS SHIT!!! Mal verliert er die Timezone
    nonzero_sect_starts = pd.Series(df[nonzero_sect_starts].index)
    nonzero_sect_ends = pd.Series(df[nonzero_sect_ends].index)
    return nonzero_sect_starts, nonzero_sect_ends