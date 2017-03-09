from .databroker import DataBroker
from .core import Images as _Images


def get_images(headers, name, handler_registry=None,
               handler_override=None):
    """
    Load images from a detector for given Header(s).

    Parameters
    ----------
    fs: FileStoreRO
    headers : Header or list of Headers
    name : string
        field name (data key) of a detector
    handler_registry : dict, optional
        mapping spec names (strings) to handlers (callable classes)
    handler_override : callable class, optional
        overrides registered handlers


    Example
    -------
    >>> header = DataBroker[-1]
    >>> images = Images(header, 'my_detector_lightfield')
    >>> for image in images:
            # do something
    """
    res = DataBroker.get_images(headers=headers, name=name,
                                handler_registry=handler_registry,
                                handler_override=handler_override)
    return res


def Images(headers, name, handler_registry=None, handler_override=None):
    """
    Load images from a detector for given Header(s).

    Parameters
    ----------
    headers : Header or list of Headers
    name : str
        field name (data key) of a detector
    handler_registry : dict, optional
        mapping spec names (strings) to handlers (callable classes)
    handler_override : callable class, optional
        overrides registered handlers

    Example
    -------
    >>> header = DataBroker[-1]
    >>> images = Images(header, 'my_detector_lightfield')
    >>> for image in images:
            # do something
    """
    return _Images(DataBroker.mds, DataBroker.fs, headers=headers, name=name,
                   handler_registry=handler_registry,
                   handler_override=handler_override)
