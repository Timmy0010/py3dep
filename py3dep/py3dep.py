"""Get data from 3DEP database."""
from __future__ import annotations

import contextlib
import io
import itertools
from typing import TYPE_CHECKING, Any, Sequence, Union, cast, overload

import async_retriever as ar
import cytoolz.curried as tlz
import geopandas as gpd
import numpy as np
import numpy.typing as npt
import pandas as pd
import pygeoutils as geoutils
import pyproj
import rioxarray._io as rxr
import shapely.ops as ops
import xarray as xr
from pygeoogc import WMS, ArcGISRESTful, ServiceURL, ZeroMatchedError
from pygeoogc import utils as ogc_utils
from pygeoutils import GeoBSpline
from rasterio import RasterioIOError
from rasterio.io import MemoryFile
from rasterio.vrt import WarpedVRT
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon

from py3dep import utils
from py3dep.exceptions import (
    InputTypeError,
    InputValueError,
    MissingCRSError,
    ServiceUnavailableError,
)

if TYPE_CHECKING:
    from pygeoutils.pygeoutils import Spline

    CRSTYPE = Union[int, str, pyproj.CRS]

LAYERS = [
    "DEM",
    "Hillshade Gray",
    "Aspect Degrees",
    "Aspect Map",
    "GreyHillshade_elevationFill",
    "Hillshade Multidirectional",
    "Slope Map",
    "Slope Degrees",
    "Hillshade Elevation Tinted",
    "Height Ellipsoidal",
    "Contour 25",
    "Contour Smoothed 25",
]
__all__ = [
    "get_map",
    "elevation_bygrid",
    "elevation_bycoords",
    "elevation_profile",
    "check_3dep_availability",
    "query_3dep_sources",
    "static_3dep_dem",
    "get_dem",
    "add_elevation",
]


@overload
def get_map(
    layers: str,
    geometry: Polygon | MultiPolygon | tuple[float, float, float, float],
    resolution: int,
    geo_crs: CRSTYPE = 4326,
    crs: CRSTYPE = 4326,
) -> xr.DataArray:
    ...


@overload
def get_map(
    layers: list[str],
    geometry: Polygon | MultiPolygon | tuple[float, float, float, float],
    resolution: int,
    geo_crs: CRSTYPE = 4326,
    crs: CRSTYPE = 4326,
) -> xr.Dataset:
    ...


def get_map(
    layers: str | list[str],
    geometry: Polygon | MultiPolygon | tuple[float, float, float, float],
    resolution: int,
    geo_crs: CRSTYPE = 4326,
    crs: CRSTYPE = 4326,
) -> xr.Dataset | xr.DataArray:
    """Access to `3DEP <https://www.usgs.gov/core-science-systems/ngp/3dep>`__ service.

    The 3DEP service has multi-resolution sources, so depending on the user
    provided resolution the data is resampled on server-side based
    on all the available data sources. The following layers are available:

    - ``DEM``
    - ``Hillshade Gray``
    - ``Aspect Degrees``
    - ``Aspect Map``
    - ``GreyHillshade_elevationFill``
    - ``Hillshade Multidirectional``
    - ``Slope Map``
    - ``Slope Degrees``
    - ``Hillshade Elevation Tinted``
    - ``Height Ellipsoidal``
    - ``Contour 25``
    - ``Contour Smoothed 25``

    Parameters
    ----------
    layers : str or list of str
        A valid 3DEP layer or a list of them.
    geometry : Polygon, MultiPolygon, or tuple
        A shapely Polygon or a bounding box of the form ``(west, south, east, north)``.
    resolution : int
        The target resolution in meters. The width and height of the output are computed in
        pixels based on the geometry bounds and the given resolution.
    geo_crs : str, int, or pyproj.CRS, optional
        The spatial reference system of the input geometry, defaults to ``EPSG:4326``.
    crs : str, int, or pyproj.CRS, optional
        The spatial reference system to be used for requesting the data, defaults to
        ``EPSG:4326``. Valid values are ``EPSG:4326``, ``EPSG:3576``, ``EPSG:3571``,
        ``EPSG:3575``, ``EPSG:3857``, ``EPSG:3572``, ``CRS:84``, ``EPSG:3573``,
        and ``EPSG:3574``.

    Returns
    -------
    xarray.DataArray or xarray.Dataset
        The requested topographic data as an ``xarray.DataArray`` or ``xarray.Dataset``.
    """
    _layers = list(layers) if isinstance(layers, (list, tuple)) else [layers]
    invalid = [lyr for lyr in _layers if lyr not in LAYERS]
    if invalid:
        raise InputValueError(f"layers ({invalid})", LAYERS)

    if "DEM" in _layers:
        _layers[_layers.index("DEM")] = "None"

    _layers = [f"3DEPElevation:{lyr}" for lyr in _layers]

    wms_url = ServiceURL().wms.nm_3dep
    valid_crs = ogc_utils.valid_wms_crs(wms_url)

    if len(valid_crs) == 0:
        raise ServiceUnavailableError(wms_url)

    if ogc_utils.validate_crs(crs).lower() not in valid_crs:
        raise InputValueError("crs", valid_crs)

    _geometry = geoutils.geo2polygon(geometry, geo_crs, crs)
    wms = WMS(wms_url, layers=_layers, outformat="image/tiff", crs=crs, validation=False)
    r_dict = wms.getmap_bybox(_geometry.bounds, resolution, box_crs=crs, max_px=10000000)

    try:
        ds = geoutils.gtiff2xarray(r_dict, _geometry, crs)
    except RasterioIOError as ex:
        raise ServiceUnavailableError(wms_url) from ex
    valid_layers = wms.get_validlayers()
    return utils.rename_layers(ds, list(valid_layers))


def get_dim_names(ds: xr.DataArray | xr.Dataset) -> tuple[str, str] | None:
    """Get vertical and horizontal dimension names."""
    y_dims = {"y", "Y", "lat", "Lat", "latitude", "Latitude"}
    x_dims = {"x", "X", "lon", "Lon", "longitude", "Longitude"}
    try:
        y_dim = list(set(ds.coords).intersection(y_dims))[0]
        y_dim = cast("str", y_dim)
        x_dim = list(set(ds.coords).intersection(x_dims))[0]
        x_dim = cast("str", x_dim)
    except IndexError:
        return None
    else:
        return (y_dim, x_dim)


def add_elevation(
    ds: xr.DataArray | xr.Dataset, ds_dims: tuple[str, str] | None = None
) -> xr.Dataset:
    """Add elevation data to a dataset  as a new variable.

    Parameters
    ----------
    ds : xarray.DataArray or xarray.Dataset
        The dataset to add elevation data to. It must contain
        CRS information.

    Returns
    -------
    xarray.Dataset
        The dataset with ``elevation``.
    """
    if not isinstance(ds, (xr.DataArray, xr.Dataset)):
        raise InputTypeError("ds", "xarray.DataArray or xarray.Dataset")

    ds_crs = ds.rio.crs
    if ds_crs is None:
        raise MissingCRSError

    if isinstance(ds, xr.DataArray):
        name = ds.name or "data"
        ds = ds.to_dataset(name=name, promote_attrs=True)
    else:
        ds = ds.copy()

    ds_dims = get_dim_names(ds)
    if ds_dims is None:
        msg = "Could not find valid dimension names in dataset. Please pass ds_dims"
        raise ValueError(msg)

    url = "/".join(
        (
            "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation",
            "13/TIFF/USGS_Seamless_DEM_13.vrt",
        )
    )
    resp = io.BytesIO(ar.retrieve_binary([url])[0])
    x, y = ds[ds_dims[1]].values, ds[ds_dims[0]].values
    with MemoryFile(resp) as memfile, memfile.open() as src:
        crs_3dep = pyproj.CRS(src.crs)
        if ds_crs != pyproj.CRS(crs_3dep):
            coords = ogc_utils.match_crs(list(itertools.product(x, y)), ds_crs, crs_3dep)
        else:
            coords = itertools.product(x, y)
        elev_arr = np.array(list(src.sample(coords))).ravel().reshape(len(y), len(x))
    elev = xr.DataArray(elev_arr, dims=ds_dims, coords={ds_dims[0]: y, ds_dims[1]: x})
    elev.attrs["long_name"] = "Elevation extracted from 3DEP at 10-m resolution"
    elev.attrs["units"] = "m"
    ds["elevation"] = elev
    return ds


def static_3dep_dem(
    geometry: Polygon | MultiPolygon | tuple[float, float, float, float],
    crs: CRSTYPE,
    resolution: int = 10,
) -> xr.DataArray:
    """Get DEM data at specific resolution from 3DEP.

    Notes
    -----
    In contrast to ``get_map`` function, this function only gets DEM data at
    specific resolution, namely 10 m, 30 m, and 60 m. However, this function
    is faster. This function is intended for cases where only need DEM at a
    specific resolution is required and for the other requests ``get_map``
    should be used.

    Parameters
    ----------
    geometry : Polygon, MultiPolygon, or tuple of length 4
        Geometry to get DEM within. It can be a polygon or a boundong box
        of form (xmin, ymin, xmax, ymax).
    crs : int, str, of pyproj.CRS
        CRS of the input geometry.
    resolution : int, optional
        Target DEM source resolution in meters, defaults to 10 m which is the highest
        resolution available over the US. Available options are 10, 30, and 60.

    Returns
    -------
    xarray.DataArray
        The request DEM at the specified resolution.
    """
    base_url = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation"
    url = {
        10: f"{base_url}/13/TIFF/USGS_Seamless_DEM_13.vrt",
        30: f"{base_url}/1/TIFF/USGS_Seamless_DEM_1.vrt",
        60: f"{base_url}/2/TIFF/USGS_Seamless_DEM_2.vrt",
    }
    if resolution not in url:
        raise InputValueError("resolution", list(url))

    resp = io.BytesIO(ar.retrieve_binary([url[resolution]])[0])
    with MemoryFile(resp) as memfile, memfile.open() as src, WarpedVRT(src) as vrt:
        dem = rxr.open_rasterio(vrt, chunks="auto")  # type: ignore
        dem = cast("xr.DataArray", dem)
        if "band" in dem.dims:
            dem = dem.squeeze("band", drop=True)
        poly = geoutils.geo2polygon(geometry, crs, vrt.crs)
        dem = dem.rio.clip_box(*poly.bounds)
        if isinstance(geometry, (Polygon, MultiPolygon)):
            dem = dem.rio.clip([poly])
        dem = dem.where(dem > dem.rio.nodata, drop=False)
        dem = dem.rio.write_nodata(np.nan)
        dem.attrs.update(
            {"units": "meters", "vertical_datum": "NAVD88", "vertical_resolution": 0.001}
        )
        dem.name = "elevation"
    return dem


def get_dem(
    geometry: Polygon | MultiPolygon | tuple[float, float, float, float],
    resolution: int,
    crs: CRSTYPE = 4326,
) -> xr.DataArray:
    """Get DEM data at any resolution from 3DEP.

    Notes
    -----
    This function is a wrapper of ``static_3dep_dem`` and ``get_map`` functions.
    Since ``static_3dep_dem`` is much faster, if the requested resolution is 10 m,
    30 m, or 60 m, ``static_3dep_dem`` will be used. Otherwise, ``get_map``
    will be used.

    Parameters
    ----------
    geometry : Polygon, MultiPolygon, or tuple of length 4
        Geometry to get DEM within. It can be a polygon or a boundong box
        of form (xmin, ymin, xmax, ymax).
    resolution : int
        Target DEM source resolution in meters.
    crs : str, int, or pyproj.CRS, optional
        The spatial reference system of the input geometry, defaults to ``EPSG:4326``.

    Returns
    -------
    xarray.DataArray
        DEM at the specified resolution in meters and 4326 CRS.
    """
    if np.isclose(resolution, (10, 30, 60)).any():
        dem = static_3dep_dem(geometry, crs, resolution)
    else:
        dem = get_map("DEM", geometry, resolution, crs)
    dem = dem.astype("f8")
    dem.attrs.update({"units": "meters", "vertical_datum": "NAVD88"})
    dem.name = "elevation"
    return dem


def elevation_bygrid(
    xcoords: Sequence[float],
    ycoords: Sequence[float],
    crs: CRSTYPE,
    resolution: int,
    depression_filling: bool = False,
) -> xr.DataArray:
    """Get elevation from DEM data for a grid.

    This function is intended for getting elevations for a gridded dataset.

    Parameters
    ----------
    xcoords : list
        List of x-coordinates of a grid.
    ycoords : list
        List of y-coordinates of a grid.
    crs : str, int, or pyproj.CRS or pyproj.CRS
        The spatial reference system of the input grid,
        defaults to ``EPSG:4326``.
    resolution : int
        The accuracy of the output, defaults to 10 m which is the highest
        available resolution that covers CONUS. Note that higher resolution
        increases computation time so chose this value with caution.
    depression_filling : bool, optional
        Fill depressions before sampling using
        `pyflwdir <https://deltares.github.io/pyflwdir>`__ package,
        defaults to ``False``.

    Returns
    -------
    xarray.DataArray
        Elevations of the input coordinates as a ``xarray.DataArray``.
    """
    pts_crs = ogc_utils.validate_crs(crs)
    points = gpd.GeoSeries(
        gpd.points_from_xy(*zip(*itertools.product(xcoords, ycoords)), crs=pts_crs)
    )
    points = points.to_crs(5070).buffer(2 * resolution)
    bbox = tuple(points.total_bounds)

    dem = get_dem(bbox, resolution, 5070)
    dem = dem.rio.reproject(pts_crs)

    if depression_filling:
        dem = utils.fill_depressions(dem)

    return dem.interp(x=list(xcoords), y=list(ycoords))


class ElevationByCoords:
    """Elevation model class.

    Parameters
    ----------
    crs : str, int, or pyproj.CRS, optional
        Coordinate reference system of the input coordinates, defaults to ``EPSG:4326``.
    coords : list of tuple
        List of coordinates.
    source : str, optional
        Elevation source, defaults to ``tep``. Valid sources are: ``tnm``, ``tep``,
        and ``airmap``.
    """

    def __init__(
        self,
        coords: list[tuple[float, float]] | tuple[float, float],
        crs: CRSTYPE = 4326,
        source: str = "tep",
    ) -> None:
        self.coords = geoutils.coords_list(coords)
        self.crs = ogc_utils.validate_crs(crs)
        self.coords_gs = gpd.GeoSeries(gpd.points_from_xy(*zip(*self.coords)), crs=self.crs)
        valid_sources = ("tnm", "airmap", "tep")
        self.source = source
        if self.source not in valid_sources:
            raise InputValueError("source", valid_sources)

    @property
    def values(self) -> list[float]:
        """Return list of elevations in meters."""
        if self.source == "tep":
            return self.tep()
        if self.source == "tnm":
            return self.tnm()
        return self.airmap()

    def airmap(self) -> list[float]:
        """Return list of elevations in meters."""
        pts = self.coords_gs.to_crs(4326)
        coords_chunks = tlz.partition_all(100, zip(pts.x, pts.y))
        headers = {"Content-Type": "application/json", "charset": "utf-8"}
        urls, kwds = zip(
            *(
                (
                    ServiceURL().restful.airmap,
                    {
                        "params": {"points": ",".join(f"{lat},{lon}" for lon, lat in chunk)},
                        "headers": headers,
                    },
                )
                for chunk in coords_chunks
            )
        )
        urls = cast("tuple[str, ...]", urls)
        kwds = cast("tuple[dict[str, Any], ...]", kwds)
        elevations = tlz.pluck("data", ar.retrieve_json(urls, kwds))
        return list(tlz.concat(elevations))

    def tnm(self) -> list[float]:
        """Return list of elevations in meters."""
        pts = self.coords_gs.to_crs(4326)
        kwds = [
            {"params": {"units": "Meters", "output": "json", "x": f"{x:.5f}", "y": f"{y:.5f}"}}
            for x, y in zip(pts.x, pts.y)
        ]
        resp = ar.retrieve_json([ServiceURL().restful.nm_pqs] * len(kwds), kwds, max_workers=5)
        resp = cast("list[dict[str, Any]]", resp)
        return [
            r["USGS_Elevation_Point_Query_Service"]["Elevation_Query"]["Elevation"] for r in resp
        ]

    def tep(self) -> list[float]:
        """Get elevation from 3DEP."""
        points_proj = self.coords_gs.to_crs(4326)
        url = "/".join(
            (
                "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation",
                "13/TIFF/USGS_Seamless_DEM_13.vrt",
            )
        )
        resp = io.BytesIO(ar.retrieve_binary([url])[0])
        with MemoryFile(resp) as memfile, memfile.open() as src:
            elev = np.array(list(src.sample(zip(points_proj.x, points_proj.y)))).ravel()
        return elev.tolist()


@overload
def elevation_bycoords(coords: tuple[float, float], crs: CRSTYPE = ..., source: str = ...) -> float:
    ...


@overload
def elevation_bycoords(
    coords: list[tuple[float, float]], crs: CRSTYPE = ..., source: str = ...
) -> list[float]:
    ...


def elevation_bycoords(
    coords: tuple[float, float] | list[tuple[float, float]],
    crs: CRSTYPE = 4326,
    source: str = "tep",
) -> float | list[float]:
    """Get elevation for a list of coordinates.

    Parameters
    ----------
    coords : tuple or list of tuple
        Coordinates of target location(s), e.g., ``[(x, y), ...]``.
    crs : str, int, or pyproj.CRS or pyproj.CRS, optional
        Spatial reference (CRS) of coords, defaults to ``EPSG:4326``.
    source : str, optional
        Data source to be used, default to ``airmap``. Supported sources are
        ``airmap`` (30 m resolution), ``tnm`` (using The National Map's Bulk Point
        Query Service with 10 m resolution) and ``tep`` (using 3DEP's WMS service
        at 10 m resolution). The ``tnm`` and ``tep`` sources are more accurate since they
        use the 1/3 arc-second DEM layer from 3DEP service but it is limited to the US.
        They both tend to be slower than the Airmap service. Note that ``tnm`` is bit unstable.
        It's recommended to use ``tep`` unless 10-m resolution accuracy is not necessary which
        in that case ``airmap`` is more appropriate.

    Returns
    -------
    float or list of float
        Elevation in meter.
    """
    _crs = crs.to_string() if isinstance(crs, pyproj.CRS) else crs
    service = ElevationByCoords(crs=_crs, coords=coords, source=source)
    if len(service.coords) == 1:
        return service.values[0]
    return service.values


def __get_spline(line: LineString, ns_pts: int, crs: CRSTYPE) -> Spline:
    """Get a B-spline from a line."""
    x, y = line.xy
    pts = gpd.GeoSeries(gpd.points_from_xy(x, y, crs=crs))
    return GeoBSpline(pts, ns_pts).spline


def __get_idx(d_sp: npt.NDArray[np.float64], distance: float) -> npt.NDArray[np.float64]:
    """Get the index of the closest point to a given distance."""
    dis = pd.DataFrame(d_sp, columns=["distance"]).reset_index()
    grouper = pd.cut(dis.distance, np.arange(0, dis.distance.max() + distance, distance))
    return dis.groupby(grouper).last()["index"].to_numpy()


def __get_spline_params(
    line: LineString, n_seg: int, distance: float, crs: CRSTYPE
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Get perpendiculars to a line."""
    _n_seg = n_seg
    spline = __get_spline(line, _n_seg, crs)
    idx = __get_idx(spline.distance, distance)
    while np.isnan(idx).any():
        _n_seg *= 2
        spline = __get_spline(line, _n_seg, crs)
        idx = __get_idx(spline.distance, distance)
    return spline.x[idx].flatten(), spline.y[idx].flatten(), spline.distance[idx].flatten()


def elevation_profile(
    lines: LineString | MultiLineString,
    spacing: float,
    dem_res: int = 10,
    crs: CRSTYPE = 4326,
) -> xr.DataArray:
    """Get the elevation profile along a line at a given uniform spacing.

    This function converts the line to a B-spline and then calculates the elevation
    along the spline at a given uniform spacing.

    Parameters
    ----------
    lines : LineString or MultiLineString
        Line segment(s) to be profiled. If its type is ``MultiLineString``,
        it will be converted to a single ``LineString`` and if this operation
        fails, a ``InputTypeError`` will be raised.
    spacing : float
        Spacing between the sample points along the line in meters.
    dem_res : float, optional
        Resolution of the DEM source to use in meter, defaults to 10.
    crs : str, int, or pyproj.CRS or pyproj.CRS, optional
        Spatial reference (CRS) of ``lines``, defaults to ``EPSG:4326``.

    Returns
    -------
    xarray.DataArray
        Elevation profile with dimension ``z`` and three coordinates: ``x``, ``y``,
        and ``distance``. The ``distance`` coordinate is the distance from the start
        of the line in meters.
    """
    if not isinstance(lines, (LineString, MultiLineString)):
        raise InputTypeError("lines", "LineString or MultiLineString")

    if isinstance(lines, MultiLineString):
        path = ops.linemerge(lines)
        if not isinstance(path, LineString):
            raise InputTypeError("lines", "mergeable to a single line")
    else:
        path = lines

    crs_prj = 5070
    geom = gpd.GeoSeries([path], crs=crs).to_crs(crs_prj)
    geom_buff = geom.buffer(5 * dem_res).unary_union.bounds
    dem = get_dem(geom_buff, dem_res, crs_prj)

    n_seg = int(np.ceil(geom.length.sum() / spacing)) * 100
    x, y, distance = __get_spline_params(geom.geometry[0], n_seg, spacing, crs_prj)
    xp, yp = zip(*ogc_utils.match_crs(list(zip(x, y)), crs_prj, dem.rio.crs))

    elevation = dem.astype("f8").interp(x=("z", list(xp)), y=("z", list(yp)))
    xp, yp = zip(*ogc_utils.match_crs(list(zip(x, y)), crs_prj, crs))
    elevation["x"], elevation["y"] = ("z", list(xp)), ("z", list(yp))
    elevation["distance"] = ("z", distance)
    return elevation


def check_3dep_availability(
    bbox: tuple[float, float, float, float], crs: CRSTYPE = 4326
) -> dict[str, bool]:
    """Query 3DEP's resolution availability within a bounding box.

    This function checks availability of 3DEP's at the following resolutions:
    1 m, 3 m, 5 m, 10 m, 30 m, 60 m, and topobathy (integrated topobathymetry).

    Parameters
    ----------
    bbox : tuple
        Bounding box as tuple of ``(min_x, min_y, max_x, max_y)``.
    crs : str, int, or pyproj.CRS or pyproj.CRS, optional
        Spatial reference (CRS) of bbox, defaults to ``EPSG:4326``.

    Returns
    -------
    dict
        True if bbox intersects 3DEP elevation for each available resolution.
        Keys are the supported resolutions and values are their availability.

    Examples
    --------
    >>> import py3dep
    >>> bbox = (-69.77, 45.07, -69.31, 45.45)
    >>> py3dep.check_3dep_availability(bbox)
    {'1m': True, '3m': False, '5m': False, '10m': True, '30m': True, '60m': False, 'topobathy': False}
    """
    if not isinstance(bbox, Sequence) or len(bbox) != 4:
        raise InputTypeError("bbox", "a tuple of length 4")

    res_layers = {
        "1m": 18,
        "3m": 19,
        "5m": 20,
        "10m": 21,
        "30m": 22,
        "60m": 23,
        "topobathy": 30,
    }
    _bbox = ogc_utils.match_crs(bbox, crs, 4326)
    url = ServiceURL().restful.nm_3dep_index

    def _check(lyr: int) -> bool:
        wms = ArcGISRESTful(url, lyr)
        with contextlib.suppress(ZeroMatchedError):
            return len(list(wms.oids_bygeom(_bbox))[0]) > 0
        return False

    return {res: _check(lyr) for res, lyr in res_layers.items()}


def query_3dep_sources(
    bbox: tuple[float, float, float, float],
    crs: CRSTYPE = 4326,
    res: str | None = None,
) -> gpd.GeoDataFrame:
    """Query 3DEP's data sources within a bounding box.

    This function queries the availability of the underlying data that 3DEP uses
    at the following resolutions:
    1 m, 3 m, 5 m, 10 m, 30 m, 60 m, and topobathy (integrated topobathymetry).

    Parameters
    ----------
    bbox : tuple
        Bounding box as tuple of ``(min_x, min_y, max_x, max_y)``.
    crs : str, int, or pyproj.CRS or pyproj.CRS, optional
        Spatial reference (CRS) of bbox, defaults to ``EPSG:4326``.
    res : str, optional
        Resolution to query, defaults to ``None``, i.e., all resolutions.

    Returns
    -------
    geopandas.GeoDataFrame
        Polygon(s) representing the 3DEP data sources at each resolution.
        Resolutions are given in the ``dem_res`` column.

    Examples
    --------
    >>> import py3dep
    >>> bbox = (-69.77, 45.07, -69.31, 45.45)
    >>> src = py3dep.query_3dep_sources(bbox)
    >>> src.groupby("dem_res")["OBJECTID"].count().to_dict()
    {'10m': 8, '1m': 3, '30m': 8}
    >>> src = py3dep.query_3dep_sources(bbox, res="1m")
    >>> src.groupby("dem_res")["OBJECTID"].count().to_dict()
    {'1m': 3}
    """
    if not isinstance(bbox, Sequence) or len(bbox) != 4:
        raise InputTypeError("bbox", "a tuple of length 4")
    res_layers = {
        "1m": 18,
        "3m": 19,
        "5m": 20,
        "10m": 21,
        "30m": 22,
        "60m": 23,
        "topobathy": 30,
    }
    if res is not None and res not in res_layers:
        raise InputValueError("res", list(res_layers.keys()))

    layers = {res: res_layers[res]} if res is not None else res_layers

    _bbox = ogc_utils.match_crs(bbox, crs, 4326)
    url = ServiceURL().restful.nm_3dep_index

    def _check(lyr: int) -> gpd.GeoDataFrame | None:
        wms = utils.RESTful(url, lyr)
        with contextlib.suppress(ZeroMatchedError):
            return wms.bygeom(_bbox)
        return None

    src = pd.concat({res: _check(lyr) for res, lyr in layers.items()})
    src = gpd.GeoDataFrame(src.reset_index(level=1, drop=True), crs=4326)
    return src.reset_index().rename(columns={"index": "dem_res"})
