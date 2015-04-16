from __future__ import absolute_import, division, print_function

import itertools
import math
import tempfile
from glob import glob
import heapq
import inspect
from pbag import PBag
from collections import Iterable, Iterator, defaultdict
import toolz
import gzip
import bz2
import os
from toolz import (merge, concat, frequencies, merge_with, take, curry, reduce,
        join, reduceby, compose, second, valmap, count, map, partition_all,
        filter, pluck, identity, groupby)
try:
    from cytoolz import (curry, frequencies, merge_with, join, reduceby,
            compose, second, count, pluck, groupby)
except ImportError:
    pass

from ..multiprocessing import get as mpget
from ..core import istask, get_dependencies, reverse_dict
from ..optimize import fuse, cull
from ..compatibility import apply
from ..context import _globals

names = ('bag-%d' % i for i in itertools.count(1))
load_names = ('load-%d' % i for i in itertools.count(1))

no_default = '__no__default__'


def lazify_task(task, start=True):
    """
    Given a task, remove unnecessary calls to ``list``

    Example
    -------

    >>> task = (sum, (list, (map, inc, [1, 2, 3])))  # doctest: +SKIP
    >>> lazify_task(task)  # doctest: +SKIP
    (sum, (map, inc, [1, 2, 3]))
    """
    if not istask(task):
        return task
    head, tail = task[0], task[1:]
    if not start and head is list:
        task = task[1]
        return lazify_task(*tail, start=False)
    else:
        return (head,) + tuple([lazify_task(arg, False) for arg in tail])


def lazify(dsk):
    """
    Remove unnecessary calls to ``list`` in tasks

    See Also:
        ``dask.bag.core.lazify_task``
    """
    return valmap(lazify_task, dsk)


def optimize(dsk, keys):
    dsk2 = cull(dsk, keys)
    dsk3 = fuse(dsk2)
    dsk4 = lazify(dsk3)
    return dsk4

def get(dsk, keys, get=None, **kwargs):
    get = get or _globals['get'] or mpget

    dsk2 = optimize(dsk, keys)

    return get(dsk2, keys, **kwargs)


def list2(seq):
    """ Another list function that won't be removed by lazify """
    return list(seq)


class Item(object):
    def __init__(self, dsk, key, get=get):
        self.dask = dsk
        self.key = key
        self.get = get

    def compute(self, **kwargs):
        return self.get(self.dask, self.key, **kwargs)

    __int__ = __float__ = __complex__ = __bool__ = compute


class Bag(object):
    """ Unordered collection with repeats

    Computed in paritions with dask

    >>> dsk = {('x', 0): (range, 5),
    ...        ('x', 1): (range, 5),
    ...        ('x', 2): (range, 5)}
    >>> b = Bag(dsk, 'x', npartitions=3)

    >>> sorted(b.map(lambda x: x * 10))  # doctest: +SKIP
    [0, 0, 0, 10, 10, 10, 20, 20, 20, 30, 30, 30, 40, 40, 40]

    >>> int(b.fold(lambda x, y: x + y))  # doctest: +SKIP
    30
    """
    def __init__(self, dsk, name, npartitions, get=get):
        self.dask = dsk
        self.name = name
        self.npartitions = npartitions
        self.get = get

    def map(self, func):
        name = next(names)
        if takes_multiple_arguments(func):
            func = curry(apply, func)
        dsk = dict(((name, i), (list, (map, func, (self.name, i))))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    def filter(self, predicate):
        name = next(names)
        dsk = dict(((name, i), (list, (filter, predicate, (self.name, i))))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    def map_partitions(self, func):
        name = next(names)
        dsk = dict(((name, i), (func, (self.name, i)))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    def pluck(self, key, default=no_default):
        name = next(names)
        if isinstance(key, list):
            key = (list2, key)
        if default == no_default:
            dsk = dict(((name, i), (list, (pluck, key, (self.name, i))))
                       for i in range(self.npartitions))
        else:
            dsk = dict(((name, i), (list, (pluck, key, (self.name, i), default)))
                       for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    def fold(self, binop, combine=None, initial=None):
        a = next(names)
        b = next(names)
        if initial:
            dsk = dict(((a, i), (reduce, binop, (self.name, i), initial))
                            for i in range(self.npartitions))
        else:
            dsk = dict(((a, i), (reduce, binop, (self.name, i)))
                            for i in range(self.npartitions))
        dsk2 = {b: (reduce, combine or binop, list(dsk.keys()))}
        return Item(merge(self.dask, dsk, dsk2), b)

    def frequencies(self):
        a = next(names)
        b = next(names)
        dsk = dict(((a, i), (frequencies, (self.name, i)))
                        for i in range(self.npartitions))
        dsk2 = {(b, 0): (dictitems,
                            (merge_with, sum, list(sorted(dsk.keys()))))}
        return Bag(merge(self.dask, dsk, dsk2), b, 1)


    def topk(self, k, key=None):
        a = next(names)
        b = next(names)
        if key:
            topk = curry(heapq.nlargest, key=key)
        else:
            topk = heapq.nlargest
        dsk = dict(((a, i), (list, (topk, k, (self.name, i))))
                        for i in range(self.npartitions))
        dsk2 = {(b, 0): (list, (topk, k, (concat, list(dsk.keys()))))}
        return Bag(merge(self.dask, dsk, dsk2), b, 1)

    def distinct(self):
        """ Distinct elements of collection

        Unordered without repeats.
        """
        a = next(names)
        dsk = dict(((a, i), (set, key)) for i, key in enumerate(self._keys()))
        b = next(names)
        dsk2 = {(b, 0): (apply, set.union, (list2, list(dsk.keys())))}

        return Bag(merge(self.dask, dsk, dsk2), b, 1)

    def _reduction(self, perpartition, aggregate):
        a = next(names)
        b = next(names)
        dsk = dict(((a, i), (perpartition, (self.name, i)))
                        for i in range(self.npartitions))
        dsk2 = {b: (aggregate, list(dsk.keys()))}
        return Item(merge(self.dask, dsk, dsk2), b)

    def sum(self):
        return self._reduction(sum, sum)

    def max(self):
        return self._reduction(max, max)

    def min(self):
        return self._reduction(min, min)

    def any(self):
        return self._reduction(any, any)

    def all(self):
        return self._reduction(all, all)

    def count(self):
        return self._reduction(count, sum)

    def mean(self):
        def chunk(seq):
            total, n = 0.0, 0
            for x in seq:
                total += x
                n += 1
            return total, n
        def agg(x):
            totals, counts = list(zip(*x))
            return 1.0 * sum(totals) / sum(counts)
        return self._reduction(chunk, agg)

    def var(self, ddof=0):
        def chunk(seq):
            squares, total, n = 0.0, 0.0, 0
            for x in seq:
                squares += x**2
                total += x
                n += 1
            return squares, total, n
        def agg(x):
            squares, totals, counts = list(zip(*x))
            x2, x, n = float(sum(squares)), float(sum(totals)), sum(counts)
            result = (x2 / n) - (x / n)**2
            return result * n / (n - ddof)
        return self._reduction(chunk, agg)

    def std(self, ddof=0):
        return math.sqrt(self.var(ddof=ddof))

    def join(self, other, on_self, on_other=None):
        assert isinstance(other, Iterable)
        assert not isinstance(other, Bag)
        if on_other is None:
            on_other = on_self
        name = next(names)
        dsk = dict(((name, i), (list, (join, on_other, other,
                                             on_self, (self.name, i))))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    def product(self, other):
        """ Cartesian product between two bags """
        assert isinstance(other, Bag)
        name = next(names)
        n, m = self.npartitions, other.npartitions
        dsk = dict(((name, i*m + j),
                   (list, (itertools.product, (self.name, i),
                                              (other.name, j))))
                   for i in range(n) for j in range(m))
        return Bag(merge(self.dask, other.dask, dsk), name, n*m)

    def foldby(self, key, binop, initial=no_default, combine=None,
               combine_initial=no_default):
        a = next(names)
        b = next(names)
        if combine is None:
            combine = binop
        if initial is not no_default:
            dsk = dict(((a, i),
                        (reduceby, key, binop, (self.name, i), initial))
                        for i in range(self.npartitions))
        else:
            dsk = dict(((a, i),
                        (reduceby, key, binop, (self.name, i)))
                        for i in range(self.npartitions))
        combine2 = lambda acc, x: combine(acc, x[1])
        if combine_initial is not no_default:
            dsk2 = {(b, 0): (dictitems,
                              (reduceby,
                                0, combine2,
                                (concat, (map, dictitems, list(dsk.keys()))),
                                combine_initial))}
        else:
            dsk2 = {(b, 0): (dictitems,
                              (merge_with,
                                (curry, reduce, combine),
                                list(dsk.keys())))}
        return Bag(merge(self.dask, dsk, dsk2), b, 1)

    def take(self, k):
        name = next(names)
        return Bag(merge(self.dask, {(name, 0): (list, (take, k, (self.name,
            0)))}), name, 1)

    def _keys(self):
        return [(self.name, i) for i in range(self.npartitions)]

    def compute(self, **kwargs):
        results = self.get(self.dask, self._keys(), **kwargs)
        if isinstance(results[0], Iterable):
            results = concat(results)
        if not isinstance(results, Iterator):
            results = iter(results)
        return results

    def concat(self):
        """ Concatenate nested lists into one long list

        >>> b = from_sequence([[1], [2, 3]])
        >>> list(b)
        [[1], [2, 3]]

        >>> list(b.concat())
        [1, 2, 3]
        """
        name = next(names)
        dsk = dict(((name, i), (list, (concat, (self.name, i))))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    __iter__ = compute

    def groupby(self, grouper, npartitions=None):
        if npartitions is None:
            npartitions = self.npartitions

        paths = [tempfile.mkdtemp('%d.pbag' % i) for i in range(npartitions)]

        # Partition data on disk
        name = next(names)
        dsk1 = dict(((name, i),
                     (partition, grouper, (self.name, i), npartitions, path))
                     for i, path in enumerate(paths))

        # Collect groups
        name = next(names)
        dsk2 = dict(((name, i),
                     (collect, grouper, npartitions, i, sorted(dsk1.keys())))
                    for i in range(npartitions))

        return Bag(merge(self.dask, dsk1, dsk2), name, npartitions)


def partition(grouper, sequence, npartitions, path):
    """ Partition a bag along a grouper, store partitions on disk """

    with PBag(grouper, npartitions, path) as pb:
        pb.extend(sequence)

    return pb


def collect(grouper, npartitions, group, pbags):
    """ Collect partitions from disk and yield k,v group pairs """
    from pbag import PBag
    result = defaultdict(list)
    for pb in pbags:
        part = pb.get_partition(group)
        groups = groupby(grouper, part)
        for k, v in groups.items():
            result[k].extend(v)
    return list(result.items())


opens = {'gz': gzip.open, 'bz2': bz2.BZ2File}


def from_filenames(filenames):
    """ Create dask by loading in lines from many files

    Provide list of filenames

    >>> b = from_filenames(['myfile.1.txt', 'myfile.2.txt'])  # doctest: +SKIP

    Or a globstring

    >>> b = from_filenames('myfiles.*.txt')  # doctest: +SKIP

    See also:
        from_sequence: A more generic bag creation function
    """
    if isinstance(filenames, str):
        filenames = sorted(glob(filenames))

    if not filenames:
        raise ValueError("No filenames found")

    extension = os.path.splitext(filenames[0])[1].strip('.')
    myopen = opens.get(extension, open)

    d = dict((('load', i), (list, (myopen, fn)))
             for i, fn in enumerate(filenames))
    return Bag(d, 'load', len(d))


def from_sequence(seq, partition_size=None, npartitions=None):
    """ Create dask from Python sequence

    This sequence should be relatively small in memory.  Dask Bag works
    best when it handles loading your data itself.  Commonly we load a
    sequence of filenames into a Bag and then use ``.map`` to open them.

    Parameters
    ----------

    seq: Iterable
        A sequence of elements to put into the dask
    partition_size: int (optional)
        The length of each partition
    npartitions: int (optional)
        The number of desired partitions

    It is best to provide either ``partition_size`` or ``npartitions``
    (though not both.)

    Example
    -------

    >>> b = from_sequence(['Alice', 'Bob', 'Chuck'], partition_size=2)

    See also:
        from_filenames: Specialized bag creation function for textfiles
    """
    seq = list(seq)
    if npartitions and not partition_size:
        partition_size = int(math.ceil(len(seq) / npartitions))
    if npartitions is None and partition_size is None:
        if len(seq) < 100:
            partition_size = 1
        else:
            partition_size = int(len(seq) / 100)

    parts = list(partition_all(partition_size, seq))
    name = next(load_names)
    d = dict(((name, i), part) for i, part in enumerate(parts))
    return Bag(d, name, len(d))


def dictitems(d):
    """ A pickleable version of dict.items """
    return list(d.items())


def takes_multiple_arguments(func):
    """

    >>> def f(x, y): pass
    >>> takes_multiple_arguments(f)
    True

    >>> def f(x): pass
    >>> takes_multiple_arguments(f)
    False

    >>> def f(x, y=None): pass
    >>> takes_multiple_arguments(f)
    False

    >>> def f(*args): pass
    >>> takes_multiple_arguments(f)
    True
    """
    try:
        spec = inspect.getargspec(func)
    except:
        return False
    if spec.varargs:
        return True
    if spec.defaults is None:
        return len(spec.args) != 1
    return len(spec.args) - len(spec.defaults) > 1
