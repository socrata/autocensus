"""A class and methods for performing Census API queries."""

import asyncio
from asyncio import Future
from contextlib import contextmanager
from csv import reader
from collections import defaultdict
from functools import partial
from io import StringIO
from itertools import chain, product
import os
from pathlib import Path
from pkg_resources import resource_string
from typing import Any, Callable, Coroutine, DefaultDict, Dict, Iterable, List, Tuple, Union

from fiona.crs import from_epsg
import pandas as pd
from pandas import DataFrame
from yarl import URL

from .api import CensusAPI, Table, look_up_census_api_key
from .geography import (
    extract_geo_type,
    load_geodataframe,
    coerce_polygon_to_multipolygon,
    flatten_geometry,
    identify_affgeoid_field
)
from .socrata import Credentials, build_dataset_name, to_socrata
from .utilities import (
    CACHE_DIRECTORY_PATH,
    chunk_variables,
    parse_table_name_from_variable,
    wrap_scalar_value_in_list,
    tidy_variable_label,
    titleize_text,
    load_annotations_dataframe
)

# Types
Tables = List[Table]
Variables = Dict[Tuple[int, str], dict]
Shapefiles = List[Path]


class Query:
    """A query for American Community Survey data from the Census API.

    A Query instance can be used to fetch ACS variables, tables, and
    shapefiles for a given ACS estimate (1-, 3-, or 5-year), year(s),
    and geographic units.
    """

    def __init__(
        self,
        estimate: int,
        years: Union[Iterable, int],
        variables: Union[Iterable, str],
        for_geo: Union[Iterable, str],
        in_geo: Iterable = None,
        join_geography: bool = True,
        table: Any = None,  # Deprecated (no longer necessary)
        census_api_key: str = None,
        verify_ssl: bool = True
    ):
        self.estimate: int = estimate
        self._years: Iterable = wrap_scalar_value_in_list(years)
        self._variables: Iterable = wrap_scalar_value_in_list(variables)
        self.for_geo: Iterable = wrap_scalar_value_in_list(for_geo)
        self.in_geo: Iterable = [] if in_geo is None else wrap_scalar_value_in_list(in_geo)
        if join_geography in [True, False]:
            self.join_geography: bool = join_geography
        else:
            raise ValueError(f'Invalid value for join_geography: {join_geography}')
        self.census_api_key: str = look_up_census_api_key(census_api_key)

        # Initialize invalid variables defaultdict
        self._invalid_variables: DefaultDict[int, list] = defaultdict(list)

        # Create cache directory if it doesn't exist
        CACHE_DIRECTORY_PATH.mkdir(exist_ok=True)

    def __repr__(self) -> str:
        attributes = ['estimate', 'years', 'variables', 'for_geo', 'in_geo']
        mappings = {attribute: repr(getattr(self, attribute)) for attribute in attributes}
        representation = (
            '<Query estimate={estimate} years={years} variables={variables} '
            'for_geo={for_geo} in_geo={in_geo}>'
        ).format_map(mappings)
        return representation

    @property
    def years(self) -> List[int]:
        """Return the supplied years as a sorted, unique list."""
        return sorted(set(self._years))

    @property
    def variables(self) -> List[str]:
        """Return the supplied variables as a sorted, unique list."""
        return sorted(set(self._variables))

    @property
    def variables_by_year_and_table_name(self) -> DefaultDict[Tuple[int, str], List[str]]:
        """Group the supplied variables by year and table name."""
        variables: DefaultDict[Tuple[int, str], List[str]] = defaultdict(list)
        for year, variable in product(self.years, self.variables):
            if variable in self._invalid_variables[year]:
                continue
            table_name: str = parse_table_name_from_variable(variable)
            variables[year, table_name].append(variable)
        return variables

    @contextmanager
    def create_census_api_session(self):
        """Initialize a Census API session.

        Adds an instance of the CensusAPI class to this Query as an
        internal attribute, then removes it upon completion.
        """
        self._census_api = CensusAPI(self.census_api_key)
        yield
        del self._census_api

    def get_variables(self) -> Variables:
        """Get labels and concepts for the supplied variables.

        Sets aside any variables that are invalid for a given year.
        """
        # Assemble API calls for concurrent execution
        calls = []
        for (year, table_name), group in self.variables_by_year_and_table_name.items():
            for variable in group:
                call: Coroutine[Any, Any, dict] = self._census_api.fetch_variable(
                    self.estimate,
                    year,
                    table_name,
                    variable
                )
                calls.append(call)

        # Make concurrent API calls
        gathered_calls = self._census_api.gather_calls(calls)
        try:
            results: Future = asyncio.run(gathered_calls)
        except RuntimeError as error:
            # Handle Jupyter issue with multiple running event loops by importing nest_asyncio
            if error.args[0] == 'asyncio.run() cannot be called from a running event loop':
                import nest_asyncio
                nest_asyncio.apply()
                results: Future = asyncio.run(gathered_calls)

        # Compile invalid variables
        variables = {}
        for variable_json in results:
            year = variable_json['year']
            if not variable_json.get('label', False):
                self._invalid_variables[year].append(variable_json['name'])
            else:
                variables[year, variable_json['name']] = variable_json
        return variables

    def get_tables(self) -> Tables:
        """Get data tables for the supplied variables."""
        # Assemble API calls for concurrent execution
        calls = []
        for (year, table_name), variables in self.variables_by_year_and_table_name.items():
            # Handle multiple for_geo values by year
            chunked_variables_by_for_geo = product(self.for_geo, chunk_variables(variables))
            for for_geo, chunk in chunked_variables_by_for_geo:
                call: Coroutine[Any, Any, Table] = self._census_api.fetch_table(
                    self.estimate,
                    year,
                    table_name,
                    chunk,
                    for_geo,
                    self.in_geo
                )
                calls.append(call)
        # Make concurrent API calls
        results: Future = asyncio.run(self._census_api.gather_calls(calls))
        tables = list(results)
        return tables

    def get_geography(self) -> Shapefiles:
        """Get shapefiles for the supplied variables and geography."""
        # Assemble API calls for concurrent execution
        calls = []
        years_with_geography = [year for year in self.years if year >= 2013]
        # Handle multiple for_geo values by year
        for year, for_geo in product(years_with_geography, self.for_geo):
            call: Coroutine[Any, Any, Path] = self._census_api.fetch_geography(
                year,
                for_geo,
                self.in_geo
            )
            calls.append(call)
        # Make concurrent API calls
        results: Future = asyncio.run(self._census_api.gather_calls(calls))
        geography = list(results)
        return geography

    def convert_variables_to_dataframe(self, variables: Variables) -> DataFrame:
        """Convert Census API variable data to a dataframe."""
        records = []
        for (year, variable_name), variable in variables.items():
            variable['year'] = year
            records.append(variable)
        dataframe = DataFrame.from_records(records)

        # Drop/rename columns, clean up label/concept values
        dataframe = dataframe \
            .drop(columns=['attributes', 'group', 'limit', 'predicateType']) \
            .rename(columns={'name': 'variable'})
        dataframe['label'] = dataframe['label'].map(tidy_variable_label)
        dataframe['concept'] = dataframe['concept'].fillna(pd.np.NaN).map(titleize_text)

        return dataframe

    def convert_tables_to_dataframe(self, tables: Tables) -> DataFrame:
        """Reshape and convert ACS data tables to a dataframe."""
        geographies: Iterable = chain(self.for_geo, self.in_geo)
        geography_types = [extract_geo_type(geo) for geo in geographies]

        # Melt each subset to adopt common schema
        subsets = []
        for header, *rows in tables:
            subset = DataFrame(rows, columns=header)
            pertinent_geography_types = set(geography_types) & set(subset.columns)
            id_vars = ['NAME', 'GEO_ID', 'geo_type', *pertinent_geography_types, 'year']
            melted: DataFrame = subset \
                .melt(id_vars=id_vars) \
                .drop(columns=pertinent_geography_types)
            subsets.append(melted)

        # Ensure correct computation of percent change and difference
        dataframe: DataFrame = pd.concat(subsets) \
            .sort_values(by=['geo_type', 'variable', 'NAME', 'year']) \
            .reset_index(drop=True)
        dataframe['value'] = dataframe['value'].astype(float)

        return dataframe

    def convert_geography_to_dataframe(self, geography: Shapefiles) -> DataFrame:
        """Convert one or more shapefiles to a dataframe using geopandas."""
        # Avoid needless encoding warnings
        os.environ['CPL_ZIP_ENCODING'] = 'UTF-8'
        subsets = []
        for filepath in geography:
            subset = load_geodataframe(filepath)
            subsets.append(subset)
        dataframe: DataFrame = pd.concat(subsets, ignore_index=True, sort=True)

        # Reproject dataframe from NAD 83 to WGS 84
        wgs_84_epsg = 4326
        dataframe['geometry'] = dataframe['geometry'].to_crs(epsg=wgs_84_epsg)
        dataframe.crs = from_epsg(wgs_84_epsg)

        # Geometry columns
        dataframe['centroid'] = dataframe.centroid
        dataframe['internal_point'] = dataframe['geometry'].representative_point()
        dataframe['geometry'] = dataframe['geometry'] \
            .map(coerce_polygon_to_multipolygon) \
            .map(flatten_geometry)

        # Clean up
        affgeoid_field = identify_affgeoid_field(dataframe.columns)
        columns_to_keep = [affgeoid_field, 'year', 'centroid', 'internal_point', 'geometry']
        dataframe = dataframe.loc[:, columns_to_keep]
        return dataframe

    def finalize_dataframe(self, dataframe: DataFrame) -> DataFrame:
        """Clean up and finalize a dataframe.

        Adds columns, normalizes column names, and reorders columns.
        """
        # Compute percent change and difference
        dataframe['percent_change'] = dataframe \
            .groupby(['GEO_ID', 'variable'])['value'] \
            .pct_change()
        dataframe['difference'] = dataframe \
            .groupby(['GEO_ID', 'variable'])['value'] \
            .diff()

        # Create year date column
        convert_datetime: Callable = partial(pd.to_datetime, format='%Y-%m-%d')
        dataframe['date'] = dataframe['year'].map('{}-12-31'.format).map(convert_datetime)

        # Rename and reorder columns
        names_csv: bytes = resource_string(__name__, 'resources/names.csv')
        csv_reader = reader(StringIO(names_csv.decode('utf-8')))
        next(csv_reader)  # Skip header row
        names: dict = dict(csv_reader)  # type: ignore
        geo_names = ['centroid', 'internal_point', 'geometry']
        if self.join_geography is True:
            name_order = [*names.values(), *geo_names]
        else:
            name_order = list(names.values())
        dataframe = dataframe.rename(columns=names)[name_order]

        return dataframe

    def assemble_dataframe(
        self,
        variables: Variables,
        tables: Tables,
        geography: Shapefiles
    ) -> DataFrame:
        """Merge and finalize the query dataframe.

        Joins tables, variables, annotations, and geospatial data, then
        cleans up and finalizes the combined dataframe.
        """
        # Merge tables with variables, annotations
        print('Merging ACS tables and variables...')
        tables_dataframe: DataFrame = self.convert_tables_to_dataframe(tables)
        variables_dataframe: DataFrame = self.convert_variables_to_dataframe(variables)
        dataframe = tables_dataframe.merge(
            right=variables_dataframe,
            how='left',
            on=['variable', 'year']
        )
        print('Merging annotations...')
        annotations_dataframe: DataFrame = load_annotations_dataframe()
        dataframe = dataframe.merge(right=annotations_dataframe, how='left', on=['value'])

        # Merge geospatial data if included
        if self.join_geography is True:
            print('Merging shapefiles...')
            geography_dataframe: DataFrame = self.convert_geography_to_dataframe(geography)
            affgeoid_field = identify_affgeoid_field(geography_dataframe.columns)
            dataframe = dataframe.merge(
                right=geography_dataframe,
                how='left',
                left_on=['GEO_ID', 'year'],
                right_on=[affgeoid_field, 'year']
            )

        # Finalize dataframe
        print('Finalizing data...')
        dataframe = self.finalize_dataframe(dataframe)

        return dataframe

    def run(self) -> DataFrame:
        """Run the supplied query and return the output as a dataframe.

        Depending on the complexity of the query, may take anywhere from
        a few seconds to several minutes.
        """
        with self.create_census_api_session():
            print('Retrieving variables...')
            variables: Variables = self.get_variables()
            print('Retrieving ACS tables...')
            tables: Tables = self.get_tables()
            if self.join_geography is True:
                print('Retrieving shapefiles...')
                geography: Shapefiles = self.get_geography()
            else:
                geography = None  # type: ignore
        dataframe = self.assemble_dataframe(variables, tables, geography)
        return dataframe

    def to_socrata(
        self,
        domain: Union[URL, str],
        *,
        dataframe: DataFrame = None,
        dataset_id: str = None,
        name: str = None,
        description: str = None,
        auth: Credentials = None,
        open_in_browser: bool = True
    ) -> URL:
        """Run query and publish the resulting dataframe to Socrata."""
        if dataframe is None:
            dataframe = self.run()
        revision_url: URL = to_socrata(
            domain,
            dataframe=dataframe,
            dataset_id=dataset_id,
            name=build_dataset_name(self.estimate, self.years) if name is None else name,
            description=description,
            auth=auth,
            open_in_browser=open_in_browser
        )
        return revision_url