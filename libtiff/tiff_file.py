"""
Provides TIFFfile class.
"""
# Author: Pearu Peterson
# Created: June 2010
from __future__ import division

__all__ = ['TIFFfile']


import sys
import numpy
from .tiff_data import type2name, name2type, type2bytes, type2dtype, tag_value2name, tag_name2value
from .utils import bytes2str
import lsm
import tif_lzw

IFDEntry_init_hooks = []
IFDEntry_finalize_hooks = []

class TIFFfile:
    """
    Hold a TIFF file image stack that is accessed via memmap.

    Attributes
    ----------
    filename : str
    data : memmap
    IFD : IFD-list
    """

    def __init__(self, filename, mode='r', first_byte = 0):
        if mode!='r':
            raise NotImplementedError(`mode`)
        self.filename = filename
        self.first_byte = first_byte
        self.data = numpy.memmap(filename, dtype=numpy.ubyte, mode=mode)

        self.memory_usage = [(self.data.nbytes, self.data.nbytes, 'eof')]
        byteorder = self.get_uint16(first_byte)
        if byteorder==0x4949:
            self.endian = 'little'
        elif byteorder==0x4d4d:
            self.endian = 'big'
        else:
            raise ValueError('unrecognized byteorder: %s' % (hex(byteorder)))
        magic = self.get_uint16(first_byte+2)
        if magic!=42:
            raise ValueError('wrong magic number for TIFF file: %s' % (magic))
        self.IFD0 = IFD0 = first_byte + self.get_uint32(first_byte+4)
        self.memory_usage.append((first_byte, first_byte+8, 'file header'))
        n = self.get_uint16(IFD0)
        IFD_list = []
        IFD_offset = IFD0
        while IFD_offset:
            n = self.get_uint16(IFD_offset)
            ifd = IFD(self)
            for i in range(n):
                entry = IFDEntry(ifd, self, IFD_offset + 2 + i*12)
                ifd.append(entry)
            ifd.finalize()
            IFD_list.append(ifd)
            self.memory_usage.append((IFD_offset, IFD_offset + 2 + n*12 + 4, 'IFD%s entries (%s)' % (len(IFD_list), len(ifd))))
            IFD_offset = self.get_uint32(IFD_offset + 2 + n*12)
        self.IFD = IFD_list

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.filename)

    def get_uint16(self, offset):
        return self.data[offset:offset+2].view(dtype=numpy.uint16)[0]
    def get_uint32(self, offset):
        return self.data[offset:offset+4].view(dtype=numpy.uint32)[0]
    def get_int16(self, offset):
        return self.data[offset:offset+2].view(dtype=numpy.int16)[0]
    def get_int32(self, offset):
        return self.data[offset:offset+4].view(dtype=numpy.int32)[0]
    def get_float32(self, offset):
        return self.data[offset:offset+4].view(dtype=numpy.float32)[0]
    def get_float64(self, offset):
        return self.data[offset:offset+8].view(dtype=numpy.float64)[0]
    get_short = get_uint16
    get_long = get_uint32
    get_double = get_float64

    def get_value(self, offset, type):
        values = self.get_values(offset, type, 1)
        if values is not None:
            return values[0]
    def get_values(self, offset, typ, count):
        if isinstance(typ, numpy.dtype):
            dtype = typ
            bytes = typ.itemsize
        elif isinstance(typ, type) and  issubclass(typ, numpy.generic):
            dtype = typ
            bytes = typ().itemsize
        else:
            if isinstance(typ, str):
                typ = name2type.get(typ)
            dtype = type2dtype.get(typ)
            bytes = type2bytes.get(typ)
            if dtype is None or bytes is None:
                sys.stderr.write('get_values: incomplete info for type=%r: dtype=%s, bytes=%s' % (typ, dtype, bytes))
                return
        return self.data[offset:offset+bytes*count].view(dtype=dtype)

    def get_string(self, offset, length = None):
        if length is None:
            i = 0
            while self.data[offset+i]:
                i += 1
            length = i
        string = self.get_values(offset, 'BYTE', length).tostring()
        return string

    def check_memory_usage(self, verbose=True):
        ''' Check memory usage of TIFF fields and blocks.

        Returns
        -------
        ok : bool
          Return False if unknown or overlapping memory areas have been detected.
        '''
        l = []
        l.extend(self.memory_usage)
        for ifd in self.IFD:
            l.extend(ifd.memory_usage)
        l.sort()
        last_end = None
        ok = True
        for start, end, resource in l:
            if last_end:
                if last_end!=start:
                    if verbose:
                        print '--- unknown %s bytes' % (start-last_end)
                    ok = False
                    if start<last_end and verbose:
                        print '--- overlapping memory area'
            if verbose:
                print '%s..%s[%s] contains %s' % (start, end,end-start, resource)
            last_end = end
        return ok

    def is_contiguous(self):
        for i,ifd in enumerate(self.IFD):
            strip_offsets = ifd.get('StripOffsets').value
            strip_nbytes = ifd.get('StripByteCounts').value
            if not ifd.is_contiguous():
                return False
            if i==0:
                pass
            else:
                if isinstance(strip_offsets, numpy.ndarray):
                    start = strip_offsets[0]
                else:
                    start = strip_offsets
                if end!=start:
                    return False
            if isinstance(strip_offsets, numpy.ndarray):
                end = strip_offsets[-1] + strip_nbytes[-1]
            else:
                end = strip_offsets + strip_nbytes
        return True

    def get_contiguous(self):
        """ Return memmap of a stack of images.
        """
        if not self.is_contiguous ():
            raise ValueError('Image stack data not contiguous')
        ifd0 = self.IFD[0]
        ifd1 = self.IFD[-1]
        width = ifd0.get ('ImageWidth').value
        length = ifd0.get ('ImageLength').value
        assert width == ifd1.get ('ImageWidth').value
        assert length == ifd1.get ('ImageLength').value
        depth = len(self.IFD)
        compression = ifd.get('Compression').value
        if compression!=1:
            raise ValueError('Unable to get contiguous image stack from compressed data')            
        bits_per_sample = ifd0.get('BitsPerSample').value
        photo_interp = ifd0.get('PhotometricInterpretation').value
        planar_config = ifd0.get('PlanarConfiguration').value        
        strip_offsets0 = ifd0.get('StripOffsets').value
        strip_nbytes0 = ifd0.get('StripByteCounts').value
        strip_offsets1 = ifd1.get('StripOffsets').value
        strip_nbytes1 = ifd1.get('StripByteCounts').value
        samples_per_pixel = ifd1.get('SamplesPerPixel').value
        assert samples_per_pixel==1,`samples_per_pixel`

        if isinstance (bits_per_sample, numpy.ndarray):
            dtype = getattr (numpy, 'uint%s' % (bits_per_sample[i]))
        else:
            dtype = getattr (numpy, 'uint%s' % (bits_per_sample))

        if isinstance(strip_offsets0, numpy.ndarray):
            start = strip_offsets0[0]
            end = strip_offsets1[-1] + strip_nbytes1[-1]
        else:
            start = strip_offsets0
            end = strip_offsets1 + strip_nbytes1
        return self.data[start:end].view (dtype=dtype).reshape ((depth, width, length))

    def get_samples(self, subfile_type=0, verbose=False):
        """
        Return samples and sample names.

        Parameters
        ----------
        subfile_type : {0, 1}
          Specify subfile type. Subfile type 1 corresponds to reduced resolution image.
        verbose : bool
          When True the print out information about samples

        Returns
        -------
        samples : list
          List of numpy.memmap arrays of samples
        sample_names : list
          List of the corresponding sample names
        """
        l = []
        i = 0
        step = 0
        can_return_memmap = True
        ifd_lst = [ifd for ifd in self.IFD if ifd.get_value('NewSubfileType', subfile_type)==subfile_type]

        depth = len(ifd_lst)
        for ifd in ifd_lst:
            if not ifd.is_contiguous():
                raise NotImplementedError('none contiguous strips')

            strip_offsets = ifd.get_value('StripOffsets')
            strip_nbytes = ifd.get_value('StripByteCounts')
            if isinstance(strip_offsets, numpy.ndarray):
                l.append((strip_offsets[0], strip_offsets[-1]+strip_nbytes[-1]))
            else:
                l.append((strip_offsets, strip_offsets+strip_nbytes))

            if i==0:
                compression = ifd.get_value('Compression')
                if compression!=1:
                    can_return_memmap = False
                    #raise ValueError('Unable to get contiguous samples from compressed data (compression=%s)' % (compression))            
                width = ifd.get_value('ImageWidth')
                length = ifd.get_value('ImageLength')
                samples_per_pixel = ifd.get_value('SamplesPerPixel', 1)
                planar_config = ifd.get_value('PlanarConfiguration')
                bits_per_sample = ifd.get_value('BitsPerSample')
                sample_format = ifd.get_value('SampleFormat')
                if self.is_lsm or not isinstance(strip_offsets, numpy.ndarray):
                    strips_per_image = 1
                else:
                    strips_per_image = len(strip_offsets)
                format = {1:'uint', 2:'int', 3:'float', None:'uint', 6:'complex'}.get(sample_format)
                if format is None:
                    print 'Warning(TIFFfile.get_samples): unsupported sample_format=%s is mapped to uint' % (sample_format)
                    format = 'uint'

                if isinstance (bits_per_sample, numpy.ndarray):
                    dtype_lst = []
                    bits_per_pixel = 0
                    for j in range(samples_per_pixel):
                        bits = bits_per_sample[j]
                        bits_per_pixel += bits
                        dtype = getattr (numpy, '%s%s' % (format, bits))
                        dtype_lst.append(dtype)
                else:
                    bits_per_pixel = bits_per_sample
                    dtype = getattr (numpy, '%s%s' % (format, bits_per_sample))
                    dtype_lst = [dtype]
                bytes_per_pixel = bits_per_pixel // 8
                assert 8*bytes_per_pixel == bits_per_pixel,`bits_per_pixel`
                bytes_per_row = width * bytes_per_pixel
                strip_length = l[-1][1] - l[-1][0]
                strip_length_str = bytes2str(strip_length)
                bytes_per_image = length * bytes_per_row
                
                rows_per_strip = bytes_per_image // (bytes_per_row * strips_per_image)
                assert rows_per_strip == ifd.get_value('RowsPerStrip', rows_per_strip), `rows_per_strip, ifd.get_value('RowsPerStrip'), bytes_per_image, bytes_per_row, strips_per_image, self.filename`
            else:
                assert width == ifd.get_value('ImageWidth', width), `width, ifd.get_value('ImageWidth')`
                assert length == ifd.get_value('ImageLength', length),` length,  ifd.get_value('ImageLength')`
                #assert samples_per_pixel == ifd.get('SamplesPerPixel').value, `samples_per_pixel, ifd.get('SamplesPerPixel').value`
                assert planar_config == ifd.get_value('PlanarConfiguration', planar_config)
                assert strip_length == l[-1][1] - l[-1][0]
                if isinstance (bits_per_sample, numpy.ndarray):
                    assert (bits_per_sample == ifd.get_value('BitsPerSample', bits_per_sample)).all(),`bits_per_sample, ifd.get_value('BitsPerSample')`
                else:
                    assert (bits_per_sample == ifd.get_value('BitsPerSample', bits_per_sample)),`bits_per_sample, ifd.get_value('BitsPerSample')`
            if i>0:
                if i==1:
                    step = l[-1][0] - l[-2][1]
                    assert step>=0,`step, l[-2], l[-1]`
                else:
                    if step != l[-1][0] - l[-2][1]:
                        can_return_memmap = False
                        #assert step == l[-1][0] - l[-2][1],`step, l[-2], l[-1], (l[-1][0] - l[-2][1]), i`
            i += 1

        if verbose:
            bytes_per_image_str = bytes2str(bytes_per_image)
            print '''
width : %(width)s
length : %(length)s
depth : %(depth)s
sample_format : %(format)s
samples_per_pixel : %(samples_per_pixel)s
planar_config : %(planar_config)s
bits_per_sample : %(bits_per_sample)s
bits_per_pixel : %(bits_per_pixel)s

bytes_per_pixel : %(bytes_per_pixel)s
bytes_per_row : %(bytes_per_row)s
bytes_per_image : %(bytes_per_image_str)s

strips_per_image : %(strips_per_image)s
rows_per_strip : %(rows_per_strip)s
strip_length : %(strip_length_str)s
''' % (locals ())

        sample_names = ['sample%s' % (j) for j in range (samples_per_pixel)]
        depth = i

        if not can_return_memmap:
            if planar_config==1:
                if samples_per_pixel==1:
                    i = 0
                    arr = numpy.empty(bytes_per_image, dtype=numpy.uint8)
                    assert len(l)==strips_per_image,`len(l), strips_per_image`
                    bytes_per_strip = bytes_per_image // strips_per_image
                    for start, end in l:
                        d = self.data[start:end]
                        if compression==5: #lzw
                            d = tif_lzw.decode(d, bytes_per_strip)
                        arr[i:i+d.nbytes] = d
                        i += d.nbytes
                    arr = arr.view(dtype=dtype_lst[0]).reshape((depth, length, width))
                    return [arr], sample_names
                else:
                    raise NotImplementedError(`samples_per_pixel`)
            else:
                raise NotImplementedError (`planar_config`)

        start = l[0][0]
        end = l[-1][1]
        if start > step:
            arr = self.data[start - step: end].reshape((depth, strip_length + step))
            k = step
        elif end <= self.data.size - step:
            arr = self.data[start: end+step].reshape((depth, strip_length + step))
            k = 0
        else:
            raise NotImplementedError (`start, end, step`)
        sys.stdout.flush()
        if planar_config==2:
            if self.is_lsm:
                # LSM510: one strip per image plane channel
                if subfile_type==0:
                    sample_names = self.lsminfo.get('data channel name')
                elif subfile_type==1:
                    sample_names = ['red', 'green', 'blue']
                    assert samples_per_pixel==3,`samples_per_pixel`
                else:
                    raise NotImplementedError (`subfile_type`)
                samples = []
                if isinstance(bits_per_sample, numpy.ndarray):
                    for j in range(samples_per_pixel):
                        bytes = bits_per_sample[j] // 8 * width * length
                        tmp = arr[:,k:k+bytes]
                        #tmp = tmp.reshape((tmp.size,))
                        tmp = tmp.view(dtype=dtype_lst[j])
                        tmp = tmp.reshape((depth, length, width))
                        samples.append(tmp)
                        k += bytes
                else:
                    assert samples_per_pixel==1,`samples_per_pixel, bits_per_sample`
                    bytes = bits_per_sample // 8 * width * length
                    tmp = arr[:,k:k+bytes]
                    #tmp = tmp.reshape((tmp.size,))
                    tmp = tmp.view(dtype=dtype_lst[0])
                    tmp = tmp.reshape((depth, length, width))
                    samples.append(tmp)
                return samples, sample_names
            raise NotImplementedError (`planar_config, self.is_lsm`)
        elif planar_config==1:
            samples = []
            if isinstance(bits_per_sample, numpy.ndarray):
                bytes = sum(bits_per_sample[:samples_per_pixel]) // 8 * width * length
            else:
                bytes = bits_per_sample // 8 * width * length
            for j in range(samples_per_pixel):
                tmp = arr[:,k+j:k+j+bytes:samples_per_pixel]
                tmp = tmp.reshape((tmp.size,)).view(dtype=dtype_lst[j])
                tmp = tmp.reshape((depth, length, width))
                samples.append(tmp)
                k += bytes
            return samples, sample_names
        else:
            raise NotImplementedError (`planar_config`)

class IFD:
    """ Image File Directory data structure.

    Attributes
    ----------
    entries : IFDEntry-list
    """
    def __init__(self, tiff):
        self.tiff = tiff
        self.entries = []

    def __len__ (self):
        return len (self.entries)

    def append(self, entry):
        self.entries.append(entry)

    @property
    def memory_usage(self):
        l = []
        for entry in self.entries:
            l.extend(entry.memory_usage)
        return l

    def __str__(self):
        l = []
        for entry in self.entries:
            l.append(str (entry))
        return '\n'.join(l)

    def get(self, tag_name):
        """Return IFD entry with given tag name.
        """
        for entry in self.entries:
            if entry.tag_name==tag_name:
                return entry
    def get_value(self, tag_name, default=None):
        """ Return the value of IFD entry with given tag name.

        When the entry does not exist, return default.
        """
        entry = self.get(tag_name)
        if entry is not None:
            return entry.value
        return default

    def finalize(self):
        for entry in self.entries:
            for hook in IFDEntry_finalize_hooks:
                hook(entry)

    def is_contiguous (self):
        strip_offsets = self.get('StripOffsets').value
        strip_nbytes = self.get('StripByteCounts').value
        if isinstance(strip_offsets, numpy.ndarray):
            for i in range (len(strip_offsets)-1):
                if strip_offsets[i] + strip_nbytes[i] != strip_offsets[i+1]:
                    return False
        return True

    def get_contiguous(self, channel_name=None):
        """ Return memmap of an image.

        This operation is succesful only when image data strips are
        contiguous in memory. Return None when unsuccesful.
        """
        width = self.get ('ImageWidth').value
        length = self.get ('ImageLength').value
        strip_offsets = self.get('StripOffsets').value
        strip_nbytes = self.get('StripByteCounts').value
        bits_per_sample = self.get('BitsPerSample').value
        photo_interp = self.get('PhotometricInterpretation').value
        planar_config = self.get('PlanarConfiguration').value
        compression = self.get('Compression').value
        subfile_type = self.get('NewSubfileType').value
        if compression != 1:
            raise ValueError('Unable to get contiguous image from compressed data')
        if not self.is_contiguous ():
            raise ValueError('Image data not contiguous')

        if self.tiff.is_lsm:
            lsminfo = self.tiff.lsminfo
            #print lsminfo
            if subfile_type==0:
                channel_names = lsminfo.get('data channel name')
            elif subfile_type==1: # thumbnails
                if photo_interp==2:
                    channel_names = 'rgb'
                else:
                    raise NotImplementedError (`photo_interp`)
            else:
                raise NotImplementedError (`subfile_type`)
            assert planar_config==2,`planar_config`
            nof_channels = self.tiff.lsmentry['DimensionChannels'][0]
            scantype = self.tiff.lsmentry['ScanType'][0]
            assert scantype==0,`scantype` # xyz-scan
            r = {}
            for i in range (nof_channels):
                if isinstance (bits_per_sample, numpy.ndarray):
                    dtype = getattr (numpy, 'uint%s' % (bits_per_sample[i]))
                    r[channel_names[i]] = self.tiff.data[strip_offsets[i]:strip_offsets[i]+strip_nbytes[i]].view (dtype=dtype).reshape((width, length))
                else:
                    dtype = getattr (numpy, 'uint%s' % (bits_per_sample))
                    r[channel_names[i]] = self.tiff.data[strip_offsets:strip_offsets+strip_nbytes].view (dtype=dtype).reshape((width, length))
            return r
        else:
            raise NotImplementedError (`self.tiff`)

class IFDEntry:
    """ Entry for Image File Directory data structure.

    Attributes
    ----------
    ifd : IFD
    tiff : TIFFfile
    tag : uint16
      data tag constant
    tag_name : str
      data tag name
    type : uint16
      data type constant
    type_name : str
      data type name
    count : uint32
      number of data points
    offset : {None, int}
      offset of a tag entry in tiff data array
    value : array
      data array
    bytes : int
      number of bytes in data array
    memory_usage : list of 3-tuples
      (start byte, end byte, name of tag)
    """
    def __init__(self, ifd, tiff, offset):
        self.ifd = ifd
        self.tiff = tiff
        self.offset = offset

        # initialization:
        self.tag = tiff.get_uint16(offset)
        self.type = tiff.get_uint16(offset+2)
        self.count = tiff.get_uint32(offset+4)
        for hook in IFDEntry_init_hooks:
            hook(self)
        
        self.bytes = bytes = type2bytes.get(self.type,0)
        if self.count==1 and 1<=bytes<=4:
            self.offset = None
            value = tiff.get_value(offset+8, self.type)
        else:
            self.offset = tiff.get_int32(offset+8)
            value = tiff.get_values(self.offset, self.type, self.count)
        if value is not None:
            self.value = value
        self.tag_name = tag_value2name.get(self.tag,'TAG%s' % (hex(self.tag),))
        self.type_name = type2name.get(self.type, 'TYPE%s' % (self.type,))

        self.memory_usage = []
        if self.offset is not None:
            self.memory_usage.append((self.offset, self.offset + self.bytes*self.count, self.tag_name))
        
    def __str__(self):
        if hasattr(self, 'str_hook'):
            r = self.str_hook(self)
            if isinstance (r, str):
                return r
        if hasattr(self, 'value'):
            return 'IFDEntry(tag=%(tag_name)s, value=%(value)r, count=%(count)s, offset=%(offset)s)' % (self.__dict__)
        else:
            return 'IFDEntry(tag=%(tag_name)s, type=%(type_name)s, count=%(count)s, offset=%(offset)s)' % (self.__dict__)

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.tiff, self.offset)

def StripOffsets_hook(ifdentry):
    if ifdentry.tag_name=='StripOffsets':
        ifd = ifdentry.ifd
        counts = ifd.get('StripByteCounts')
        if ifdentry.offset is not None:
            for i, (count, offset) in enumerate(zip(counts.value, ifdentry.value)):
                ifdentry.memory_usage.append((offset, offset+count, 'strip %s' % (i)))
        else:
            offset = ifdentry.value
            ifdentry.memory_usage.append((offset, offset+counts.value, 'strip'))

# todo: TileOffsets_hook

IFDEntry_finalize_hooks.append(StripOffsets_hook)

# Register CZ LSM support:
lsm.register(locals())