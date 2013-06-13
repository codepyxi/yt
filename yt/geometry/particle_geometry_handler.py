"""
Particle-only geometry handler

Author: Matthew Turk <matthewturk@gmail.com>
Affiliation: Columbia University
Homepage: http://yt-project.org/
License:
  Copyright (C) 2012 Matthew Turk.  All Rights Reserved.

  This file is part of yt.

  yt is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 3 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import h5py
import numpy as na
import string, re, gc, time, cPickle
import weakref

from itertools import chain, izip

from yt.funcs import *
from yt.utilities.logger import ytLogger as mylog
from yt.arraytypes import blankRecordArray
from yt.config import ytcfg
from yt.data_objects.field_info_container import NullFunc
from yt.geometry.geometry_handler import GeometryHandler, YTDataChunk
from yt.geometry.oct_container import \
    ParticleOctreeContainer
from yt.utilities.definitions import MAXLEVEL
from yt.utilities.io_handler import io_registry
from yt.utilities.parallel_tools.parallel_analysis_interface import \
    ParallelAnalysisInterface, parallel_splitter

from yt.data_objects.data_containers import data_object_registry

class ParticleGeometryHandler(GeometryHandler):

    def __init__(self, pf, data_style):
        self.data_style = data_style
        self.parameter_file = weakref.proxy(pf)
        # for now, the hierarchy file is the parameter file!
        self.hierarchy_filename = self.parameter_file.parameter_filename
        self.directory = os.path.dirname(self.hierarchy_filename)
        self.float_type = np.float64
        super(ParticleGeometryHandler, self).__init__(pf, data_style)

    def _setup_geometry(self):
        mylog.debug("Initializing Particle Geometry Handler.")
        self._initialize_particle_handler()


    def get_smallest_dx(self):
        """
        Returns (in code units) the smallest cell size in the simulation.
        """
        raise NotImplementedError

    def convert(self, unit):
        return self.parameter_file.conversion_factors[unit]

    def _initialize_particle_handler(self):
        self._setup_data_io()
        template = self.parameter_file.filename_template
        ndoms = self.parameter_file.file_count
        cls = self.parameter_file._file_class
        self.data_files = [cls(self.parameter_file, self.io, template % {'num':i}, i)
                           for i in range(ndoms)]
        total_particles = sum(sum(d.total_particles.values())
                              for d in self.data_files)
        self.oct_handler = ParticleOctreeContainer(
            [1, 1, 1],
            self.parameter_file.domain_left_edge,
            self.parameter_file.domain_right_edge)
        self.oct_handler.n_ref = 64
        mylog.info("Allocating for %0.3e particles", total_particles)
        for dom in self.data_files:
            self.io._initialize_octree(dom, self.oct_handler)
        self.oct_handler.finalize()
        self.max_level = self.oct_handler.max_level
        tot = sum(self.oct_handler.recursively_count().values())
        mylog.info("Identified %0.3e octs", tot)

    def _detect_fields(self):
        # TODO: Add additional fields
        pfl = []
        for dom in self.data_files:
            fl = self.io._identify_fields(dom)
            dom._calculate_offsets(fl)
            for f in fl:
                if f not in pfl: pfl.append(f)
        self.field_list = pfl
        pf = self.parameter_file
        pf.particle_types = tuple(set(pt for pt, pf in pfl))
        pf.particle_types += ('all',)

    def _setup_classes(self):
        dd = self._get_data_reader_dict()
        super(ParticleGeometryHandler, self)._setup_classes(dd)
        self.object_types.sort()

    def _identify_base_chunk(self, dobj):
        if getattr(dobj, "_chunk_info", None) is None:
            mask = dobj.selector.select_octs(self.oct_handler)
            counts = self.oct_handler.count_cells(dobj.selector, mask)
            subsets = [ParticleDomainSubset(d, mask, c)
                       for d, c in zip(self.domains, counts) if c > 0]
            dobj._chunk_info = subsets
            dobj.size = sum(counts)
            dobj.shape = (dobj.size,)
        dobj._current_chunk = list(self._chunk_all(dobj))[0]

    def _chunk_all(self, dobj):
        oobjs = getattr(dobj._current_chunk, "objs", dobj._chunk_info)
        yield YTDataChunk(dobj, "all", oobjs, dobj.size)

    def _chunk_spatial(self, dobj, ngz, sort = None):
        sobjs = getattr(dobj._current_chunk, "objs", dobj._chunk_info)
        for i,og in enumerate(sobjs):
            if ngz > 0:
                g = og.retrieve_ghost_zones(ngz, [], smoothed=True)
            else:
                g = og
            size = og.cell_count
            if size == 0: continue
            yield YTDataChunk(dobj, "spatial", [g], size)

    def _chunk_io(self, dobj):
        oobjs = getattr(dobj._current_chunk, "objs", dobj._chunk_info)
        for subset in oobjs:
            yield YTDataChunk(dobj, "io", [subset], subset.cell_count)
