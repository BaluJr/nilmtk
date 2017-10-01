from __future__ import print_function, division
import pandas as pd
from datetime import timedelta
import matplotlib.pyplot as plt
from ..results import Results
from nilmtk.timeframe import TimeFrame, convert_none_to_nat, convert_nat_to_none
from nilmtk.utils import get_tz, tz_localize_naive
from nilmtk.timeframegroup import TimeFrameGroup

class NonZeroSectionsResults(Results):
    """
    Attributes
    ----------
    _data : pd.DataFrame
        index is start date for the whole chunk
        `end` is end date for the whole chunk
        `sections` is a TimeFrameGroups object (a list of nilmtk.TimeFrame objects)
    """
    
    name = "nonzero_sections"

    def __init__(self):
        super(NonZeroSectionsResults, self).__init__()

    def append(self, timeframe, new_results):
        """Append a single result.

        Parameters
        ----------
        timeframe : nilmtk.TimeFrame
        new_results : {'sections': list of TimeFrame objects}
        """
        new_results['sections'] = [TimeFrameGroup(new_results['sections'][0])]
        super(NonZeroSectionsResults, self).append(timeframe, new_results)

    def combined(self):
        """ Merges together any nonzero sections which span multiple segments.
        Whether there are gaps in between does not matter.

        Returns
        -------
        sections : TimeFrameGroup (a subclass of Python's list class)
        """

        sections = TimeFrameGroup()
        for index, row in self._data.iterrows():
            row_sections = row['sections']

            # Check if first TimeFrame of row_sections needs to be merged with
            # last TimeFrame of previous section (Then it is set to None as described in 'get_nonzero_sections')
            
                #if row_sections[0].start is None:
                # Prev ends with None and current starts with None -> merge

            # When there was a None at prev end -> Merge
            if len(sections) > 1 and sections[-1].end is None:
                sections[-1].end = row_sections[0].end
                row_sections.pop(0)
                # Prev ends not with None and current starts with None -> new section
                #else:
                #    row_sections[0].start = index 
                #else:
                #    # Current starts not with None but previous -> End section in last section
                #    if sections and sections[-1].end is None:
                #        try:
                #            sections[-1].end = end_date_of_prev_row
                #        except ValueError: # end_date_of_prev_row before sections[-1].start
                #            pass
                
            sections.extend(row_sections)

        if sections:
            sections[-1].include_end = True
            if sections[-1].end is None:
                sections[-1].end = row['end']
     
        return sections

    def unify(self, other):
        super(NonZeroSectionsResults, self).unify(other)
        for start, row in self._data.iterrows():
            other_sections = other._data['sections'].loc[start]
            intersection = row['sections'].intersection(other_sections)
            self._data['sections'].loc[start] = intersection

    def to_dict(self):
        nonzero_sections = self#.combined()
        nonzero_sections_list_of_dicts = [timeframe.to_dict() 
                                       for timeframe in nonzero_sections]
        return {'statistics': {'nonzero_sections': nonzero_sections_list_of_dicts}}

    def plot(self, **plot_kwargs):
        timeframes = self#.combined()
        return timeframes.plot(**plot_kwargs)
        
    def import_from_cache(self, cached_stat, sections):   
        # HIER IST DAS PROBLEM BEIM STATISTIKEN LESEN! DIE WERDEN CHUNK Weise GESPEICHERT, aber hier wird auf das Vorhandensein der gesamten Section als ganzes vertraut
        '''
        As explained in 'export_to_cache' the sections have to be stored 
        rowwise. This function parses the lines and rearranges them as a 
        proper NonZeroSectionsResult again.
        '''
        # we (deliberately) use duplicate indices to cache NonZeroSectionResults
        grouped_by_index = cached_stat.groupby(level=0)
        tz = get_tz(cached_stat)
        for tf_start, df_grouped_by_index in grouped_by_index:
            grouped_by_end = df_grouped_by_index.groupby('end')
            for tf_end, sections_df in grouped_by_end:
                end = tz_localize_naive(tf_end, tz)
                timeframe = TimeFrame(tf_start, end)
                if any([section.contains(timeframe) for section in sections]): # Had to adapt this, because otherwise no cache use when loaded in chunks
                    timeframes = []
                    for _, row in sections_df.iterrows():
                        section_start = tz_localize_naive(row['section_start'], tz)
                        section_end = tz_localize_naive(row['section_end'], tz)
                        timeframes.append(TimeFrame(section_start, section_end))
                    self.append(timeframe, {'sections': [timeframes]})

    def export_to_cache(self):
        """
        Returns the DataFrame to be written into cache.

        Returns
        -------
        DataFrame with three columns: 'end', 'section_end', 'section_start'.
            Instead of storing a list of TimeFrames on each row,
            we store one TimeFrame per row.  This is because pd.HDFStore cannot
            save a DataFrame where one column is a list if using 'table' format'.
            We also need to strip the timezone information from the data columns.
            When we import from cache, we assume the timezone for the data 
            columns is the same as the tz for the index.
        """
        index_for_cache = []
        data_for_cache = [] # list of dicts with keys 'end', 'section_end', 'section_start'
        for index, row in self._data.iterrows():
            for section in row['sections']:
                index_for_cache.append(index)
                data_for_cache.append(
                    {'end': row['end'], 
                     'section_start': convert_none_to_nat(section.start),
                     'section_end': convert_none_to_nat(section.end)})
        df = pd.DataFrame(data_for_cache, index=index_for_cache)
        return df.convert_objects()
