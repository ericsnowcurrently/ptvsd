import sys


_builtins = list(__builtins__.values())

if sys.version_info >= (3,):
    unicode = str
    file = object()
    buffer = memoryview
    rangetype = range
    ziptype = zip
    maptype = map
    filtertype = filter
else:
    Ellipsis = object()
    rangetype = xrange  # noqa
    ziptype = list
    maptype = list
    filtertype = list


def decode_recursive(obj):
    """Return a copy of the object with all bytes converted to unicode.

    Note that user-defined types (including subclasses of builtin types)
    are currently unsupported.  Likewise, exception objects (instances
    of BaseException) are not supported.
    """
    if isinstance(obj, unicode):
        return obj
    elif isinstance(obj, bytes):
        return obj.decode('utf-8')

    # Containers that need conversion:
    elif type(obj) == dict:
        copied = {}
        for key, value in obj.items():
            key = decode_recursive(key)
            copied[key] = decode_recursive(value)
        return copied
    elif type(obj) == set:
        return {decode_recursive(item) for item in obj}
    elif type(obj) == frozenset:
        return frozenset(decode_recursive(item) for item in obj)
    elif type(obj) == tuple:
        return tuple(decode_recursive(item) for item in obj)
    elif type(obj) == list:
        return [decode_recursive(item) for item in obj]

    # Containers that don't need conversion:
    if isinstance(obj, bytearray):
        return obj
    elif isinstance(obj, (memoryview, buffer)):
        return obj

    # Non-containers:
    if type(obj) is object:
        return obj
    elif obj in (None, True, False, Ellipsis, NotImplemented):
        return obj
    elif type(obj) in (int, float, complex):
        return obj
    elif type(obj) in (rangetype, slice):
        return obj
    elif type(obj) is file:
        return obj
    elif type(obj) is type(decode_recursive):  # functions
        return obj
    elif type(obj) is (property, classmethod, staticmethod):
        return obj
    elif type(obj) is super:
        return obj
    elif obj in _builtins:
        if isinstance(obj, type):
            return obj
        # All the other builtins should be covered already.

    # We haven't decided about the following:
    if isinstance(obj, type):
        raise NotImplementedError
    elif isinstance(obj, BaseException):
        raise NotImplementedError
    elif type(obj) in (enumerate, reversed, ziptype, maptype, filtertype):
        raise NotImplementedError

    # default
    raise NotImplementedError
