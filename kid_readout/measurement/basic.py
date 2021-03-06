"""
This module contains basic measurement classes for data acquired with the ROACH.
"""
from __future__ import division
import time
from collections import OrderedDict
import logging

import numpy as np
import pandas as pd
from matplotlib.pyplot import mlab
from scipy import signal
from memoized_property import memoized_property

from kid_readout.measurement import core
from kid_readout.analysis.resonator import lmfit_resonator
from kid_readout.analysis.timeseries import binning, despike, iqnoise, periodic
from kid_readout.roach import calculate

logger = logging.getLogger(__name__)


class RoachMeasurement(core.Measurement):
    """
    An abstract base class for measurements taken with the ROACH.
    """

    _version = 0

    def start_epoch(self):
        """
        Return self.epoch, if it exists, and if not return the earliest epoch of any RoachMeasurement that this
        measurement contains. Measurements that are not RoachMeasurements are ignored.

        Returns
        -------
        float
            The epoch of this Measurement or the earliest epoch of its contents; np.nan if neither is found.
        """
        if hasattr(self, 'epoch'):
            return self.epoch
        else:
            possible_epochs = []
            public_nodes = [(k, v) for k, v in self.__dict__.items()
                            if not k.startswith('_') and isinstance(v, core.Node)]
            for name, node in public_nodes:
                if isinstance(node, RoachMeasurement):
                    possible_epochs.append(node.start_epoch())
                elif isinstance(node, core.MeasurementList):
                    possible_epochs.append(np.min([m.start_epoch() for m in node if isinstance(m, RoachMeasurement)]))
            if possible_epochs:
                return np.min(possible_epochs)
            else:
                return np.nan

    def _delete_memoized_property_caches(self):
        for attr in dir(self):
            if hasattr(self, '_' + attr) and attr not in core.PRIVATE:
                delattr(self, '_' + attr)
        if self._parent is not None:
            self._parent._delete_memoized_property_caches()

    @property
    def cryostat(self):
        try:
            return self._io.metadata.cryostat
        except (AttributeError, KeyError) as e:
            return None


class RoachStream(RoachMeasurement):

    _version = 1

    def __init__(self, tone_bin, tone_amplitude, tone_phase, tone_index, filterbank_bin, epoch, sequence_start_number,
                 s21_raw, data_demodulated, roach_state, state=None, description='', validate=True):
        """
        Return a new RoachStream instance. This class has no dimensions and is intended to be subclassed.

        Parameters
        ----------
        tone_bin : numpy.ndarray(int)
            An array of integers representing the frequencies of the tones played during the measurement.
        tone_amplitude : numpy.ndarray(float)
            An array of floats representing the amplitudes of the tones played during the measurement.
        tone_phase : numpy.ndarray(float)
            An array of floats representing the radian phases of the tones played during the measurement.
        tone_index : int or numpy.ndarray(int)
            tone_bin[tone_index] corresponds to the frequency used to produce s21_raw.
        filterbank_bin : int or numpy.ndarray(int)
            The filter bank bin(s) containing the tone(s).
        epoch : float
            The unix timestamp of the first sample of the time-ordered data.
        sequence_start_number : int
            The ROACH sequence number for the first sample of the time-ordered data.
        s21_raw : numpy.ndarray(complex)
            The data, demodulated or not.
        data_demodulated : bool
            True if the data is demodulated.
        roach_state : dict
            State information for the roach; the result of roach.state.
        state : dict
            All non-roach state information.
        description : str
            A human-readable description of this measurement.
        validate : bool
            If True, check that the array shapes match the dimensions OrderedDict.
        """
        self.tone_bin = tone_bin
        self.tone_amplitude = tone_amplitude
        self.tone_phase = tone_phase
        self.tone_index = tone_index
        self.filterbank_bin = filterbank_bin
        self.epoch = epoch
        self.sequence_start_number = sequence_start_number
        self.s21_raw = s21_raw
        self.data_demodulated = data_demodulated
        self.roach_state = core.StateDict(roach_state)
        super(RoachStream, self).__init__(state=state, description=description, validate=validate)

    @memoized_property
    def sample_time(self):
        """numpy.ndarray(float): The time of each sample relative to the first sample in the stream."""
        return (np.arange(self.s21_raw.shape[-1], dtype='float') /
                self.stream_sample_rate)

    @memoized_property
    def frequency(self):
        return calculate.frequency(self.roach_state, self.tone_bin[self.tone_index])

    @property
    def frequency_MHz(self):
        return 1e-6 * self.frequency

    @memoized_property
    def baseband_frequency(self):
        return calculate.baseband_frequency(self.roach_state, self.tone_bin[self.tone_index])

    @property
    def baseband_frequency_MHz(self):
        return 1e-6 * self.baseband_frequency

    @property
    def stream_sample_rate(self):
        return calculate.stream_sample_rate(self.roach_state)

    @memoized_property
    def s21_raw_mean(self):
        """
        Return the mean of s21_raw for each channel. NaN samples are excluded from the calculation.

        Raises
        ------
        RuntimeWarning
            If all of the samples for a channel are NaN; in this case the return value for that channel will be NaN.

        Returns
        -------
        numpy.ndarray(complex)
           The mean of s21_raw for each channel.
        """
        return np.nanmean(self.s21_raw, axis=-1)

    @memoized_property
    def s21_raw_mean_error(self):
        """
        Estimate the error in s21_raw_mean for each channel. NaN samples are excluded from the calculation.

        The method assumes that the samples are independent, which is probably not true.

        Raises
        ------
        RuntimeWarning
            If all of the samples for a channel are NaN; in this case the return value for that channel will be NaN.

        Returns
        -------
        numpy.ndarray(complex)
            An estimate of the complex standard error of the mean of s21_raw.
        """
        # The float cast allows conversion to NaN.
        num_good_samples = np.sum(~np.isnan(self.s21_raw), axis=-1).astype(np.float)
        if isinstance(num_good_samples, np.ndarray):
            # Avoid a ZeroDivisionError if some of the channels have no good samples.
            num_good_samples[num_good_samples == 0] = np.nan
        elif num_good_samples == 0:  # num_good_samples is a scalar; avoid a ZeroDivisionError.
            num_good_samples = np.nan
        return ((np.nanstd(self.s21_raw.real, axis=-1) + 1j * np.nanstd(self.s21_raw.imag, axis=-1)) /
                np.sqrt(num_good_samples))

    @property
    def s21_point(self):
        """
        Return one s21 point per stream, calculated using the best available method.

        The method used to calculate this point may change.
        """
        return self.s21_raw_mean

    @property
    def s21_point_error(self):
        """
        Return an estimate of the standard error of the mean for s21_point, calculated using the best available method.

        The method used to calculate this error may change.
        """
        return self.s21_raw_mean_error

    def fold(self, array, period_samples=None, reduce=np.mean):
        if period_samples is None:
            period_samples = calculate.modulation_period_samples(self.roach_state)
        return periodic.fold(array, period_samples, reduce=reduce)

    def epochs(self, start=-np.inf, stop=np.inf):
        """
        Return a StreamArray containing only the data between the given start and stop epochs.

        The returned StreamArray has the same state.

        The indexing follows the Python convention that the first value is inclusive and the second is exclusive:
        start <= epoch < stop
        Thus, the two slices stream_array.epochs(t0, t1) and stream_array.epochs(t1, t2) will contain all the data
        occurring at or after t0 and before t2, with no duplication.
        """
        start_index = np.searchsorted(self.epoch + self.sample_time, (start,), side='left')[0] # Need to be ints
        stop_index = np.searchsorted(self.epoch + self.sample_time, (stop,), side='right')[0]  # This index not included
        return self.__class__(tone_bin=self.tone_bin, tone_amplitude=self.tone_amplitude,
                              tone_phase=self.tone_phase, tone_index=self.tone_index,
                              filterbank_bin=self.filterbank_bin, epoch=self.epoch + self.sample_time[start_index],
                              sequence_start_number=np.nan,  # This may no longer be valid for the sliced data.
                              s21_raw=self.s21_raw[..., start_index:stop_index],
                              data_demodulated=self.data_demodulated, roach_state=self.roach_state,
                              state=self.state, description=self.description)


class RoachStream0(RoachMeasurement):
    """
    This class is a factory for producing RoachStream version 1 instances from version 0 data.

    Version 0 did not save the sequence_start_number integer.
    """
    def __new__(cls, *args, **kwargs):
        kwargs['sequence_start_number'] = None
        return RoachStream(*args, **kwargs)


class StreamArray(RoachStream):
    """
    This class represents simultaneously-sampled data from multiple channels.
    """

    _version = 1

    dimensions = OrderedDict([('tone_bin', ('tone_bin',)),
                              ('tone_amplitude', ('tone_bin',)),
                              ('tone_phase', ('tone_bin',)),
                              ('tone_index', ('tone_index',)),
                              ('filterbank_bin', ('tone_index',)),
                              ('s21_raw', ('tone_index', 'sample_time'))])

    def __init__(self, tone_bin, tone_amplitude, tone_phase, tone_index, filterbank_bin, epoch, sequence_start_number,
                 s21_raw, data_demodulated, roach_state, state=None, description='', validate=True):
        """
        Return a new StreamArray instance. The integer array tone_index contains the indices of tone_bin,
        tone_amplitude, and tone_phase for the tones demodulated to produce the time-ordered s21_raw data.
        The tone_bin, tone_amplitude, tone_phase, tone_index, and filterbank_bin arrays are 1-D, while s21_raw is 2-D:
        s21_raw.shape == (tone_index.size, sample_time.size)

        Parameters
        ----------
        tone_bin : numpy.ndarray(int)
            An array of integers representing the frequencies of the tones played during the measurement.
        tone_amplitude : numpy.ndarray(float)
            An array of floats representing the amplitudes of the tones played during the measurement.
        tone_phase : numpy.ndarray(float)
            An array of floats representing the radian phases of the tones played during the measurement.
        tone_index : numpy.ndarray(int)
            tone_bin[tone_index] corresponds to the frequency used to produce s21_raw.
        filterbank_bin : numpy.ndarray(int)
            The filter bank bins in which the tones lie.
        epoch : float
            The unix timestamp of the first sample of the time-ordered data.
        sequence_start_number : int
            The ROACH sequence number for the first sample of the time-ordered data.
        s21_raw : numpy.ndarray(complex)
            The data, demodulated or not.
        data_demodulated : bool
            True if the data is demodulated.
        roach_state : dict
            State information for the roach; the result of roach.state.
        state : dict
            All non-roach state information.
        description : str
            A human-readable description of this measurement.
        validate : bool
            If True, check that the array shapes match the dimensions OrderedDict.
        """
        super(StreamArray, self).__init__(tone_bin=tone_bin, tone_amplitude=tone_amplitude, tone_phase=tone_phase,
                                          tone_index=tone_index, filterbank_bin=filterbank_bin, epoch=epoch,
                                          sequence_start_number=sequence_start_number, s21_raw=s21_raw,
                                          data_demodulated=data_demodulated, roach_state=roach_state, state=state,
                                          description=description, validate=validate)

    def __getitem__(self, number):
        """
        See stream().
        """
        number = int(number)  # Avoid weird indexing bugs
        ss = SingleStream(tone_bin=self.tone_bin, tone_amplitude=self.tone_amplitude, tone_phase=self.tone_phase,
                          tone_index=self.tone_index[number], filterbank_bin=self.filterbank_bin[number],
                          epoch=self.epoch, sequence_start_number=self.sequence_start_number,
                          s21_raw=self.s21_raw[number, :], data_demodulated=self.data_demodulated,
                          roach_state=self.roach_state, number=number, state=self.state,
                          description=self.description)
        ss._io = self._io
        ss._io_node_path = self._io_node_path
        return ss

    def stream(self, number):
        """
        Return a SingleStream object containing the data from the channel corresponding to the given integer.

        Parameters
        ----------
        number : int
            The index of the stream to use to create the new single-channel object.

        Returns
        -------
        SingleStream
        """
        return self[number]

    def tone_offset_frequency(self, normalized_frequency=True):
        offset = calculate.tone_offset_frequency(self.tone_bin,self.roach_state.num_tone_samples,self.filterbank_bin,
                                     self.roach_state.num_filterbank_channels)
        if not normalized_frequency:
            offset = offset * self.stream_sample_rate
        return offset

    def to_dataframe(self, add_origin = False):
        dataframes = []
        for number in range(self.tone_bin.shape[0]):
            dataframes.append(self.stream(number).to_dataframe(add_origin=add_origin))
        return pd.concat(dataframes, ignore_index=True)

class StreamArray0(RoachStream0):
    """
    This class is a factory for producing StreamArray version 1 instances from version 0 data.

    Version 0 did not save the sequence_start_number integer.
    """
    def __new__(cls, *args, **kwargs):
        kwargs['sequence_start_number'] = None
        return StreamArray(*args, **kwargs)


class SingleStream(RoachStream):
    """
    This class contains time-ordered data from a single channel.
    """

    _version = 1

    dimensions = OrderedDict([('tone_bin', ('tone_bin',)),
                              ('tone_amplitude', ('tone_bin',)),
                              ('tone_phase', ('tone_bin',)),
                              ('s21_raw', ('sample_time',))])

    def __init__(self, tone_bin, tone_amplitude, tone_phase, tone_index, filterbank_bin, epoch, sequence_start_number,
                 s21_raw, data_demodulated, roach_state, number=None, state=None, description='', validate=True):
        """
        Return a new SingleStream instance. The single integer tone_index is the common index of tone_bin,
        tone_amplitude, and tone_phase for the tone used to produce the time-ordered s21_raw data.

        The tone_bin, tone_amplitude, tone_phase, tone_index, filterbank_bin, and s21_raw arrays are all 1-D.

        Parameters
        ----------
        tone_bin : numpy.ndarray(int)
            An array of integers representing the frequencies of the tones played during the measurement.
        tone_amplitude : numpy.ndarray(float)
            An array of floats representing the amplitudes of the tones played during the measurement.
        tone_phase : numpy.ndarray(float)
            An array of floats representing the radian phases of the tones played during the measurement.
        tone_index : int
            tone_bin[tone_index] corresponds to the frequency used to produce s21_raw.
        filterbank_bin : int
            An int that is the filter bank bin in which the tone lies.
        epoch : float
            The unix timestamp of the first sample of the time-ordered data.
        sequence_start_number : int
            The ROACH sequence number for the first sample of the time-ordered data.
        s21_raw : numpy.ndarray(complex)
            The data, demodulated or not.
        data_demodulated : bool
            True if the data is demodulated.
        roach_state : dict
            State information for the roach; the result of roach.state.
        number : int or None
            The number of this instance in some larger structure, such as a StreamArray.
        state : dict
            All non-roach state information.
        description : str
            A human-readable description of this measurement.
        validate : bool
            If True, check that the array shapes match the dimensions OrderedDict.
        """
        self.number = number
        super(SingleStream, self).__init__(tone_bin=tone_bin, tone_amplitude=tone_amplitude, tone_phase=tone_phase,
                                           tone_index=tone_index, filterbank_bin=filterbank_bin, epoch=epoch,
                                           sequence_start_number=sequence_start_number, s21_raw=s21_raw,
                                           data_demodulated=data_demodulated, roach_state=roach_state, state=state,
                                           description=description, validate=validate)

    # ToDo: use tone_index?
    def tone_offset_frequency(self, normalized_frequency=True):
        offset = calculate.tone_offset_frequency(self.tone_bin,self.roach_state.num_tone_samples,self.filterbank_bin,
                                                 self.roach_state.num_filterbank_channels)
        if not normalized_frequency:
            offset = offset * self.stream_sample_rate
        return offset

    def to_dataframe(self, add_origin=True):
        data = {'number': self.number, 'analysis_epoch': time.time(), 'start_epoch': self.start_epoch()}
        try:
            for key, value in self.roach_state.items():
                data['roach_{}'.format(key)] = value
        except KeyError:
            pass

        data.update(self.state.flatten(wrap_lists=True))

        data['s21_point'] = [self.s21_point]
        data['s21_point_error'] = [self.s21_point_error]
        data['frequency'] = [self.frequency]
        data['frequency_MHz'] = [self.frequency_MHz]

        dataframe = pd.DataFrame(data, index=[0])
        if add_origin:
            self.add_origin(dataframe)
        return dataframe


class SingleStream0(RoachStream0):
    """
    This class is a factory for producing SingleStream version 1 instances from version 0 data.

    Version 0 did not save the sequence_start_number integer.
    """
    def __new__(cls, *args, **kwargs):
        kwargs['sequence_start_number'] = None
        return SingleStream(*args, **kwargs)


class SweepArray(RoachMeasurement):
    """
    This class contains a list of StreamArrays.

    The analysis methods and properties are designed for two modes of operation.

    For simultaneous sweeps across individual resonators, use the sweep() method (equivalent to __getitem__ access)
    to return SingleSweep instances containing the data from individual resonators.

    For scans over many unknown resonators, use the properties to return values in ascending frequency order, and use
    find_resonators() and resonator() to locate resonance frequencies and extract resonator objects.
    """

    _version = 0

    def __init__(self, stream_arrays, state=None, description=''):
        """
        Parameters
        ----------
        stream_arrays : iterable(StreamArray)
            The streams that make up the sweep.
        state : dict
            All non-roach state information.
        description : str
            A human-readable description of this measurement.
        """
        if not isinstance(stream_arrays, core.MeasurementList):
            stream_arrays = core.MeasurementList(stream_arrays)
        self.stream_arrays = stream_arrays
        super(SweepArray, self).__init__(state=state, description=description)

    def __getitem__(self, number):
        """
        See sweep().
        """
        number = int(number)  # Avoid weird indexing bugs
        ss = SingleSweep(streams=core.MeasurementList(sa.stream(number) for sa in self.stream_arrays),
                           number=number, state=self.state, description=self.description)
        ss._io = self._io
        ss._io_node_path = self._io_node_path
        return ss

    def sweep(self, number):
        """
        Return a SingleSweep object containing the data from the channel corresponding to the given integer.

        Parameters
        ----------
        number : int
            The index of the sweep to use to create the new single-channel object.

        Returns
        -------
        SingleSweep
        """
        return self[number]

    @property
    def num_channels(self):
        try:
            if np.any(np.diff([sa.tone_index.size for sa in self.stream_arrays])):
                raise ValueError("Channel numbers differ between stream arrays.")
            else:
                return self.stream_arrays[0].tone_index.size
        except IndexError:
            return 0

    @memoized_property
    def ascending_order(self):
        """numpy.ndarray[int]: Re-arranges values for this SweepArray in ascending frequency order."""
        return np.concatenate([sa.frequency for sa in self.stream_arrays]).argsort()

    @memoized_property
    def frequency(self):
        """numpy.ndarray[float]: The frequencies of all data points in ascending order."""
        return np.concatenate([sa.frequency for sa in self.stream_arrays])[self.ascending_order]

    @property
    def frequency_MHz(self):
        """numpy.ndarray[float]: The frequencies in MHz of all data points, in ascending order."""
        return 1e-6 * self.frequency

    @memoized_property
    def s21_point(self):
        """numpy.ndarray[complex]: The s21_point values of all data points, in ascending frequency order."""
        return np.concatenate([sa.s21_point for sa in self.stream_arrays])[self.ascending_order]

    @memoized_property
    def s21_point_error(self):
        """numpy.ndarray[complex]: The s21_point_error values of all data points, in ascending frequency order."""
        return np.concatenate([sa.s21_point_error for sa in self.stream_arrays])[self.ascending_order]

    @memoized_property
    def s21_raw(self):
        """numpy.ndarray[complex]: The raw s21 streams of all data points, in ascending frequency order."""
        return np.vstack([stream_array.s21_raw for stream_array in self.stream_arrays])[self.ascending_order, :]

    @property
    def s21_point_foreground(self):
        return self.s21_point / self.background

    @property
    def s21_point_error_foreground(self):
        return self.s21_point_error / self.background

    @property
    def background(self):
        return self.fit_background(frequency=self.frequency, s21=self.s21_point)

    # ToDo: swap this out for a lmfit model
    def fit_background(self, frequency=None, s21=None, amp_degree=3, phi_degree=3, weights=None, mask=None):
        if frequency is None:
            frequency = self.frequency
        if s21 is None:
            s21 = self.s21_point
        if weights is None:  # Attempt to down-weight resonances
            weights = np.abs(s21) ** 2
        if mask is None:
            mask = np.ones(frequency.size, dtype=np.bool)
        weights *= mask
        amp_poly = np.polyfit(frequency, np.abs(s21), deg=amp_degree, w=weights)
        phi_poly = np.polyfit(frequency, np.unwrap(np.angle(s21)), deg=phi_degree, w=weights)
        return np.polyval(amp_poly, frequency) * np.exp(1j * np.polyval(phi_poly, frequency))

    # ToDo: this could be much smarter about identifying large peaks
    def find_peaks(self, expected_Q=30000, num_widths=100, threshold=1, minimum_linewidth_separation=1,
                   **find_peaks_cwt_kwargs):
        linewidth = self.frequency.mean() / expected_Q
        width = linewidth / (self.frequency[1] - self.frequency[0])  # in samples
        data = 1 / np.abs(self.s21_point_foreground) - 1
        widths = np.linspace(width / 10, 10 * width, num_widths)
        peaks = np.array(signal.find_peaks_cwt(vector=data, widths=widths, **find_peaks_cwt_kwargs))
        mask = (width < peaks) & (peaks < data.size - width) & (data[peaks] > threshold * np.std(data))
        return peaks[mask]

    def resonator(self, frequency, width, model=lmfit_resonator.LinearLossResonatorWithCable):
        mask = (frequency - width / 2 <= self.frequency) & (self.frequency <= frequency + width / 2)
        return model(frequency=self.frequency[mask], s21=self.s21_point_foreground[mask],
                     errors=self.s21_point_error_foreground[mask])

    def to_dataframe(self, add_origin=True, one_sweep_per_row=True):
        """

        Parameters
        ----------
        add_origin
        one_sweep_per_row: bool, default True
            If True, return a dataframe with one row per sweep, usually what you want if each sweep corresponds to a
            resonator.
            If False, return a single row with frequency and s21_point arrays for VNA style sweep data.

        Returns
        -------
        pandas.DataFrame
        """
        dataframes = []
        if one_sweep_per_row:
            for number in range(self.num_channels):
                dataframes.append(self.sweep(number).to_dataframe(add_origin=add_origin))
        else:
            data = {'analysis_epoch': time.time(), 'start_epoch': self.start_epoch()}
            try:
                for thermometer, temperature in self.state['temperature'].items():
                    data['temperature_{}'.format(thermometer)] = temperature
            except KeyError:
                pass
            try:
                for key, value in self.stream_arrays[0].stream(0).roach_state.items():
                    data['roach_{}'.format(key)] = value
            except KeyError:
                pass

            flat_state = self.state.flatten(wrap_lists=True)
            data.update(flat_state)
            data['frequency'] = [self.frequency]
            data['s21_point'] = [self.s21_point]
            data['s21_point_error'] = [self.s21_point_error]
            dataframe = pd.DataFrame(data, index=[0])
            self.add_origin(dataframe)
            dataframes = [dataframe]
        return pd.concat(dataframes, ignore_index=True)


class SingleSweep(RoachMeasurement):
    """
    This class contains a list of SingleStreams with different frequencies.

    The properties return values in ascending frequency order.
    """

    _version = 0

    def __init__(self, streams, number=None, state=None, description=''):
        """
        Parameters
        ----------
        streams: iterable(SingleStream)
            The streams that make up the sweep.
        number : int or None
            The number of this instance in the SweepArray from which it was created.
        state : dict
            All non-roach state information.
        description : str
            A human-readable description of this measurement.
        """
        if not isinstance(streams, core.MeasurementList):
            streams = core.MeasurementList(streams)
        self.streams = streams
        self.number = number
        super(SingleSweep, self).__init__(state=state, description=description)

    @memoized_property
    def ascending_order(self):
        """numpy.ndarray[int]: Re-arranges values for this SingleSweep in ascending frequency order."""
        return np.array([s.frequency for s in self.streams]).argsort()

    @memoized_property
    def frequency(self):
        """numpy.ndarray[float]: The frequencies of all data points in ascending order."""
        return np.array([s.frequency for s in self.streams])[self.ascending_order]

    @property
    def frequency_MHz(self):
        """numpy.ndarray[float]: The frequencies in MHz of all data points, in ascending order."""
        return 1e-6 * self.frequency

    @memoized_property
    def s21_point(self):
        """numpy.ndarray[complex]: The s21_point values of all data points, in ascending frequency order."""
        return np.array([s.s21_point for s in self.streams])[self.ascending_order]

    @memoized_property
    def s21_point_error(self):
        """numpy.ndarray[complex]: The s21_point_error values of all data points, in ascending frequency order."""
        return np.array([s.s21_point_error for s in self.streams])[self.ascending_order]

    @memoized_property
    def s21_raw(self):
        """numpy.ndarray[complex]: The raw s21 streams of all data points, in ascending frequency order."""
        return np.vstack([s.s21_raw for s in self.streams])[self.ascending_order, :]

    @memoized_property
    def s21_normalized(self):
        return np.array([self.resonator.remove_background(f, s21) for f, s21 in zip(self.frequency, self.s21_point)])

    @memoized_property
    def s21_normalized_error(self):
        return np.array([self.resonator.remove_background(f, s21_error)
                         for f, s21_error in zip(self.frequency, self.s21_point_error)])

    @memoized_property
    def resonator(self):
        """BaseResonator: the result of the last call to fit_resonator()."""
        return self.fit_resonator()

    def fit_resonator(self, model=lmfit_resonator.LinearResonatorWithCable, params=None):
        """
        Fit the s21 data with the given resonator model and, if given, the initial Parameters.

        Parameters
        ----------
        model : BaseResonator
            The resonator model to use for the fit.
        params : lmfit.Parameters
            A parameters object to use for initial values and limits in the fit.
        """
        self._delete_memoized_property_caches()
        self._resonator = model(frequency=self.frequency, s21=self.s21_point, errors=self.s21_point_error)
        self._resonator.fit(params)
        return self._resonator

    def to_dataframe(self, add_origin=True):
        data = {'number': self.number, 'analysis_epoch': time.time(), 'start_epoch': self.start_epoch()}
        try:
            for key, value in self.streams[0].roach_state.items():
                data['roach_{}'.format(key)] = value
        except KeyError:
            pass

        data.update(self.state.flatten(wrap_lists=True))

        for param in self.resonator.current_result.params.values():
            data['res_{}'.format(param.name)] = param.value
            data['res_{}_error'.format(param.name)] = param.stderr
        data['res_redchi'] = self.resonator.current_result.redchi
        data['res_Q_i'] = self.resonator.Q_i
        data['res_Q_e'] = self.resonator.Q_e

        data['res_s21_data'] = [self.resonator.data]
        data['res_frequency_data'] = [self.resonator.frequency]
        data['res_s21_errors'] = [self.resonator.errors]
        modelf = np.linspace(self.resonator.frequency.min(), self.resonator.frequency.max(), 1000)
        models21 = self.resonator.model.eval(params=self.resonator.current_params, f=modelf)
        data['res_model_frequency'] = [modelf]
        data['res_model_s21'] = [models21]

        dataframe = pd.DataFrame(data, index=[0])
        if add_origin:
            self.add_origin(dataframe)
        return dataframe


class SweepStreamArray(RoachMeasurement):

    _version = 0

    def __init__(self, sweep_array, stream_array, state=None, description=''):
        """
        Parameters
        ----------
        sweep_array : SweepArray
        stream_array : StreamArray
        state : dict
        description : str
        """
        if sweep_array.num_channels != stream_array.tone_index.size:
            raise core.MeasurementError("The number of SweepArray channels does not match the StreamArray number.")
        self.sweep_array = sweep_array
        self.stream_array = stream_array
        super(SweepStreamArray, self).__init__(state=state, description=description)

    def epochs(self, start=-np.inf, stop=np.inf):
        """
        Return a new SweepStreamArray containing the same sweep and only the stream data between the given epochs.

        Parameters
        ----------
        start : float
            The start time of the slice.
        stop : float
            The stop time of the slice.

        Returns
        -------
        SweepStreamArray
        """
        return SweepStreamArray(self.sweep_array, self.stream_array.epochs(start, stop), state=self.state,
                                description=self.description)

    @property
    def num_channels(self):
        return self.sweep_array.num_channels

    def __getitem__(self, number):
        """
        See sweep_stream()
        """
        number = int(number)  # Avoid weird indexing bugs
        sss = SingleSweepStream(sweep=self.sweep_array.sweep(number), stream=self.stream_array.stream(number),
                                number=number, state=self.state, description=self.description)
        sss._io = self._io
        sss._io_node_path = self._io_node_path
        return sss

    def sweep_stream(self, number):
        """
        Return a SingleSweepStream object containing the data from the channel corresponding to the given integer.

        Parameters
        ----------
        number : int
            The index of the sweep and stream to use to create the new single-channel object.

        Returns
        -------
        SingleSweepStream
        """
        return self[number]

    def to_dataframe(self):
        dataframes = []
        for number in range(self.num_channels):
            dataframes.append(self.sweep_stream(number).to_dataframe())
        return pd.concat(dataframes, ignore_index=True)


class SingleSweepStream(RoachMeasurement):

    _version = 0

    def __init__(self, sweep, stream, number=None, state=None, description=''):
        """
        Parameters
        ----------
        sweep : SingleSweep
        stream : SingleStream
        number : int or None
        state : dict
        description : str
        """
        self.sweep = sweep
        self.stream = stream
        self.number = number
        super(SingleSweepStream, self).__init__(state=state, description=description)

    def epochs(self, start=-np.inf, stop=np.inf):
        """
        Return a new SingleSweepStream containing the same sweep and only the stream data between the given epochs.

        Parameters
        ----------
        start : float
            The start time of the slice.
        stop : float
            The stop time of the slice.

        Returns
        -------
        SingleSweepStream
        """
        return SingleSweepStream(self.sweep, self.stream.epochs(start, stop), state=self.state,
                                 number=self.number, description=self.description)

    @property
    def resonator(self):
        return self.sweep.resonator

    @property
    def glitch_mask(self):
        if not hasattr(self,'_glitch_mask'):
            logger.debug("glitch_mask running deglitch")
            self.deglitch()
        return self._glitch_mask

    @property
    def number_of_masked_samples(self):
        if not hasattr(self,'_number_of_masked_samples'):
            logger.debug("number_of_masked_samples running deglitch")
            self.deglitch()
        return self._number_of_masked_samples

    def deglitch(self, threshold=8, window_in_seconds=1, mask_extend_samples=50):
        window_samples = int(2 ** np.ceil(np.log2(window_in_seconds * self.stream.stream_sample_rate)))
        logger.debug("deglitching with threshold %f, window %.f seconds, %d samples, extending mask by %d samples"
                     % (threshold, window_in_seconds,window_samples, mask_extend_samples))
        try:
            self._glitch_mask = despike.deglitch_mask_mad(self.x_raw, thresh=threshold, window_length=window_samples,
                                                          mask_extend=mask_extend_samples)
            self._number_of_masked_samples = self._glitch_mask.sum()
            self._x, self._q, self._stream_s21_normalized_deglitched = despike.mask_glitches([self.x_raw, self.q_raw,
                                                                                              self.stream_s21_normalized],
                                                                                             mask=self._glitch_mask,
                                                                                             window_length=window_samples)
        except:
            self._glitch_mask = np.zeros(self.x_raw.shape, dtype='bool')
            self._number_of_masked_samples = self._glitch_mask.sum()
            self._x = self.x_raw
            self._q = self.q_raw
            self._stream_s21_normalized_deglitched = self.stream_s21_normalized

        logger.debug("masked %d samples out of %d total, fraction: %f" % (self._number_of_masked_samples,
                                                                          self._x_raw.size,
                                                                          (self._number_of_masked_samples /
                                                                           self._x_raw.size)))

    @memoized_property
    def stream_s21_normalized(self):
        return self.sweep.resonator.remove_background(self.stream.frequency, self.stream.s21_raw)

    @property
    def stream_s21_normalized_deglitched(self):
        if not hasattr(self,'_stream_s21_normalized_deglitched'):
            logger.debug("stream_s21_normalized_deglitch running deglitch")
            self.deglitch()
        return self._stream_s21_normalized_deglitched

    @property
    def q_raw(self):
        """
        Return the inverse internal quality factor q = 1 / Q_i calculated by inverting the resonator model.

        Returns
        -------
        numpy.ndarray (float)
            Values of q from self.stream corresponding to self.stream.sample_time
        """
        if not hasattr(self, '_q_raw'):
            self.set_raw_q_and_x()
        return self._q_raw

    @property
    def y_raw(self):
        """
        Return half the inverse internal quality factor y = q / 2 calculated by inverting the resonator model. The
        purpose of this is that S_yy = S_xx when amplifier-noise dominated.

        Returns
        -------
        numpy.ndarray (float)
            Values of y from self.stream corresponding to self.stream.sample_time.
        """
        return self.q_raw / 2

    @property
    def x_raw(self):
        """
        Return the fractional frequency shift x = f / f_r - 1 calculated by inverting the resonator model.

        Returns
        -------
        numpy.ndarray (float)
            Values of x from self.stream corresponding to self.stream.sample_time.
        """
        if not hasattr(self, '_x_raw'):
            self.set_raw_q_and_x()
        return self._x_raw

    @property
    def q(self):
        """
        Return the inverse internal quality factor q = 1 / Q_i calculated by inverting the resonator model.

        Returns
        -------
        numpy.ndarray (float)
            Values of q from self.stream corresponding to self.stream.sample_time
        """
        if not hasattr(self, '_q'):
            self.deglitch()
        return self._q

    @property
    def y(self):
        """
        Return half the inverse internal quality factor y = q / 2 calculated by inverting the resonator model. The
        purpose of this is that S_yy = S_xx when amplifier-noise dominated.

        Returns
        -------
        numpy.ndarray (float)
            Values of y from self.stream corresponding to self.stream.sample_time.
        """
        return self.q / 2

    @property
    def x(self):
        """
        Return the fractional frequency shift x = f / f_r - 1 calculated by inverting the resonator model.

        Returns
        -------
        numpy.ndarray (float)
            Values of x from self.stream corresponding to self.stream.sample_time.
        """
        if not hasattr(self, '_x'):
            self.deglitch()
        return self._x

    def set_raw_q_and_x(self):
        """
        Use the resonator model to calculate time-ordered resonator parameters from the time-ordered s21 data.

        The parameters are q and x: q is the inverse internal quality factor and x is the fractional frequency shift.

        Returns
        -------
        None
        """
        self._x_raw, self._q_raw = self.resonator.invert(self.stream_s21_normalized)

    @property
    def S_frequency(self):
        """
        Return the frequencies used in calculating the single-sided spectral densities.

        Returns
        -------
        numpy.ndarray (float)
             Positive frequencies through the Nyquist frequency.
        """
        if not hasattr(self, '_S_frequency'):
            self.set_S()
        return self._S_frequency

    @property
    def S_qq(self):
        """
        The single-sided spectral density of q(t), S_qq(f), where f is self.S_frequency.

        Returns
        -------
        numpy.ndarray (complex)
            :return: an array of complex values representing the spectral density of q(t)
        """
        if not hasattr(self, '_S_qq'):
            self.set_S()
        return self._S_qq

    @property
    def S_yy(self):
        """
        The single-sided spectral density of y(t), S_yy(f), where f is self.S_frequency.
        :return: an array of complex values representing the spectral density of y(t)
        """
        return self.S_qq / 4

    @property
    def S_xx(self):
        """
        The single-sided spectral density of x(t), S_xx(f), where f is self.S_frequency.
        :return: an array of complex values representing the spectral density of x(t)
        """
        if not hasattr(self, '_S_xx'):
            self.set_S()
        return self._S_xx

    @property
    def S_xq(self):
        """
        The single-sided cross-spectral density of q(t) and x(t), S_xq(f), where f is self.S_frequency.

        Returns
        -------
        numpy.ndarray (complex)
        """
        if not hasattr(self, '_S_xq'):
            self.set_S()
        return self._S_xq

    @property
    def S_xy(self):
        """
        The single-sided cross-spectral density of y(t) and x(t), S_yx(f), where f is self.S_frequency.

        Returns
        -------
        numpy.ndarray (complex)
        """
        return self.S_xq / 2

    @property
    def S_edges(self):
        """
        The edges of the bins used for log-binning of the spectral densities, or None if the spectra were not binned.

        Returns
        -------
        numpy.ndarray(float)
        """
        if not hasattr(self, '_S_edges'):
            self.set_S()
        return self._S_edges

    @property
    def S_counts(self):
        """
        The number of spectral samples that contributed to each frequency bin
        Returns
        -------

        """
        if not hasattr(self,'_S_counts'):
            self.set_S()
        return self._S_counts

    @property
    def S_xx_variance(self):
        """
        The variance on S_xx
        Returns
        -------

        """
        if not hasattr(self,'_S_xx_variance'):
            self.set_S()
        return self._S_xx_variance

    @property
    def S_qq_variance(self):
        """
        The variance on S_qq
        Returns
        -------

        """
        if not hasattr(self,'_S_qq_variance'):
            self.set_S()
        return self._S_qq_variance

    @property
    def S_yy_variance(self):
        """
        The variance on S_yy
        Returns
        -------

        """
        return self.S_qq_variance / 16

    def set_S(self, NFFT=None, window=mlab.window_none, detrend=mlab.detrend_none, noverlap=None, binned=True,
              bins_per_decade=30, masking_function=None, **psd_kwds):
        """
        Calculate the spectral density of self.x and self.q and set the related properties.

        Parameters
        ----------
        NFFT : int or None
            The number of samples to use for each FFT chunk; should be a power of two for speed; if None, a reasonable
            default is calculated that averages about eight spectra.
        detrend : callable
            A function that takes a complex time series as argument and returns a detrended time series.
        window  : callable
            A function that takes a complex time series as argument and returns a windowed time series.
        noverlap : int or None
            The number of samples to overlap in each chunk; if None, a value equal to half the NFFT value is used.
        binned : bool
            If True, the result is binned using bin sizes that increase with frequency.
        bins_per_decade : int
            If binned is True, this is the number of frequency bins per decade that will be used.
        masking_function : callable
            A function that takes the frequency and all spectral densities as inputs and produces a boolean mask used
            to remove points from them.
        psd_kwds : dict
            Additional keywords to pass to mlab.psd and mlab.csd.

        Returns
        -------
        None
        """
        if NFFT is None:
            NFFT = int(2**(np.floor(np.log2(self.stream.s21_raw.size)) - 3))
        if noverlap is None:
            noverlap = NFFT // 2
        S_qq, f = mlab.psd(self.q, Fs=self.stream.stream_sample_rate, NFFT=NFFT, window=window, detrend=detrend,
                           noverlap=noverlap, **psd_kwds)
        S_xx, f = mlab.psd(self.x, Fs=self.stream.stream_sample_rate, NFFT=NFFT, window=window, detrend=detrend,
                           noverlap=noverlap, **psd_kwds)
        S_xq, f = mlab.csd(self.x, self.q, Fs=self.stream.stream_sample_rate, NFFT=NFFT, window=window, detrend=detrend,
                           noverlap=noverlap, **psd_kwds)
        if masking_function is not None:
            mask = masking_function(f, S_xx, S_qq, S_xq)
            f = f[mask]
            S_qq = S_qq[mask]
            S_xx = S_xx[mask]
            S_xq = S_xq[mask]
            self._S_mask = mask
            logger.debug("Masked %d frequencies from raw power spectra" % (~mask).sum())
        # Drop the DC and Nyquist bins since they're not helpful and make plots look messy.
        f = f[1:-1]
        S_xx = S_xx[1:-1]
        S_qq = S_qq[1:-1]
        S_xq = S_xq[1:-1]
        # The value in each bin is chi-squared distributed with degrees of freedom equal to two times the number of
        # spectra that are averaged. Assume that the variance of the value in a bin is equal to the square of the value
        # in that bin divided by the number of degrees of freedom. Using nonzero overlap will complicate this, but let's
        # ignore that. It's also not clear that this is correct for the cross-spectrum.
        ndof = 2 * self.x.size // NFFT
        if binned:
            edges, counts, f_mean, d_and_v = binning.log_bin_with_variance(f, bins_per_decade,
                                                                           (S_xx, S_xx**2 / ndof),
                                                                           (S_qq, S_qq**2 / ndof),
                                                                           (S_xq, S_xq**2 / ndof))
            (S_xx, V_xx), (S_qq, V_qq), (S_xq, V_xq) = d_and_v
        else:
            edges = None
            counts = np.ones(f.size, dtype=int)
            f_mean = f
            V_xx = S_xx**2 / ndof
            V_qq = S_qq**2 / ndof
            V_xq = S_xq**2 / ndof
        self._S_edges = edges
        self._S_counts = counts
        self._S_frequency = f_mean
        self._S_qq = S_qq
        self._S_xx = S_xx
        self._S_xq = S_xq
        self._S_xx_variance = V_xx
        self._S_qq_variance = V_qq
        self._S_xq_variance = V_xq

    @property
    def pca_S_frequency(self):
        if not hasattr(self, '_pca_frequency'):
            self.set_pca()
        return self._pca_S_frequency

    @property
    def pca_S_00(self):
        if not hasattr(self, '_pca_S_00'):
            self.set_pca()
        return self._pca_S_00

    @property
    def pca_S_11(self):
        if not hasattr(self, '_pca_S_11'):
            self.set_pca()
        return self._pca_S_11

    @property
    def pca_angles(self):
        if not hasattr(self, '_pca_angles'):
            self.set_pca()
        return self._pca_angles

    def set_pca(self, NFFT=None, window=mlab.window_none, detrend=mlab.detrend_none, binned=True):
        """
        Calculate the spectral densities of the complex time series self.x + 1j * self.y in the two directions that,
        at each point, correspond to minimal and maximal fluctuation. We use self.y = self.q / 2 here because
        isotropic fluctuations in S_{21} have the same amplitude in these units.

        Note that if `binned` is True then binning is performed before PCA, and this can produce quite different results
        from binning after PCA. At frequencies where amplifier noise dominates, PCA without binning will select the
        direction corresponding to the largest fluctuations, so pca_S_00 will be less than pca_S_11 even though the
        noise is isotropic.

        Parameters
        ----------
        NFFT : int
            The number of samples to use for each FFT chunk; should be a power of two for speed.
        window  : callable
            A function that takes a complex time series as argument and returns a windowed time series.
        detrend : callable
            A function that takes a complex time series as argument and returns a detrended time series.
        binned : bool
            If True, the PSDs are binned using bin sizes that increase with frequency before PCA is performed.

        Returns
        -------
        None
        """
        if NFFT is None:
            NFFT = int(2**(np.floor(np.log2(self.stream.s21_raw.size)) - 3))
        fr, S, evals, evects, angles, piq = iqnoise.pca_noise(self.x + 1j * self.y, NFFT=NFFT,
                                                              Fs=self.stream.stream_sample_rate, window=window,
                                                              detrend=detrend, use_log_bins=binned,
                                                              use_full_spectral_helper=True)
        self._pca_S_frequency = fr
        self._pca_S_00 = evals[0]
        self._pca_S_11 = evals[1]
        self._pca_angles = angles

    def to_dataframe(self, add_origin=True, num_model_points=1000):
        data = {'number': self.number, 'analysis_epoch': time.time(), 'start_epoch': self.start_epoch()}

        data.update(self.state.flatten(wrap_lists=True))
        data.update(self.stream.state.flatten(prefix='stream', wrap_lists=True))
        data.update(self.sweep.state.flatten(prefix='sweep', wrap_lists=True))

        try:
            for key, value in self.stream.roach_state.items():
                data['roach_{}'.format(key)] = value
        except KeyError:
            pass

        data['stream_frequency'] = [self.stream.frequency]

        for param in self.resonator.current_result.params.values():
            data['res_{}'.format(param.name)] = param.value
            data['res_{}_error'.format(param.name)] = param.stderr
        data['res_redchi'] = self.resonator.current_result.redchi
        data['res_Q_i'] = self.resonator.Q_i
        data['res_Q_e'] = self.resonator.Q_e

        data['res_frequency_data'] = [self.resonator.frequency]
        data['res_s21_data'] = [self.resonator.data]
        data['res_s21_errors'] = [self.resonator.errors]
        model_f = np.linspace(self.resonator.frequency.min(), self.resonator.frequency.max(), num_model_points)
        data['res_model_frequency'] = [model_f]
        model_s21 = self.resonator.model.eval(params=self.resonator.current_params, f=model_f)
        data['res_model_s21'] = [model_s21]
        data['res_s21_data_normalized'] = [self.resonator.remove_background(self.resonator.frequency,
                                                                            self.resonator.data)]
        data['res_model_s21_normalized'] = [self.resonator.remove_background(model_f, model_s21)]
        s21_at_f_0 = self.resonator.model.eval(params=self.resonator.current_params, f=self.resonator.f_0)
        data['res_model_s21_at_f_0'] = s21_at_f_0
        data['res_model_s21_normalized_at_f_0'] = self.resonator.remove_background(self.resonator.f_0, s21_at_f_0)

        try:
            data['folded_x'] = [self.stream.fold(self.x_raw)]
            data['folded_q'] = [self.stream.fold(self.q_raw)]
            data['folded_normalized_s21'] = [self.stream.fold(self.stream_s21_normalized)]
        except ValueError:
            pass

        data['S_xx'] = [self.S_xx]
        data['S_yy'] = [self.S_yy]
        data['S_xy'] = [self.S_xy]
        data['S_xx_variance'] = [self.S_xx_variance]
        data['S_yy_variance'] = [self.S_yy_variance]
        data['S_counts'] = [self.S_counts]
        #data['S_xy_variance'] = [self.S_xy_variance]
        data['S_frequency'] = [self.S_frequency]

        dataframe = pd.DataFrame(data, index=[0])
        if add_origin:
            self.add_origin(dataframe)
        return dataframe

    def fold(self, array, period_samples=None, reduce=np.mean):
        if period_samples is None:
            period_samples = calculate.modulation_period_samples(self.stream.roach_state)
        return periodic.fold(array, period_samples, reduce=reduce)


class SweepStreamList(RoachMeasurement):

    _version = 0

    def __init__(self, sweep, stream_list, state=None, description=''):
        """
        Parameters
        ----------
        sweep : SweepArray
        stream_list : iterable(StreamArray)
        state : dict
        description : str
        """
        self.sweep = sweep
        if not isinstance(stream_list, core.MeasurementList):
            stream_list = core.MeasurementList(stream_list)
        self.stream_list = stream_list
        super(SweepStreamList, self).__init__(state=state, description=description)

    def __getitem__(self, number):
        """
        See single_sweep_stream_list().
        """
        number = int(number)  # Avoid weird indexing bugs
        sssl = SingleSweepStreamList(self.sweep[number],
                                     core.MeasurementList(sa[number] for sa in self.stream_list),
                                     number=number, state=self.state, description=self.description)
        sssl._io = self._io
        sssl._io_node_path = self._io_node_path
        return sssl

    def single_sweep_stream_list(self, number):
        """
        Return a SingleSweepStreamList object containing the data from the channel corresponding to the given integer.

        Parameters
        ----------
        number : int
            The index of the sweep and streams to use to create the new single-channel object.

        Returns
        -------
        SingleSweepStreamList
        """
        return self[number]


class SingleSweepStreamList(RoachMeasurement):

    _version = 0

    def __init__(self, single_sweep, stream_list, number=None, state=None, description=''):
        """
        Parameters
        ----------
        single_sweep : SingleSweep
        stream_list : iterable(SingleStream)
        number : int or None
        state : dict
        description : str
        """
        self.single_sweep = single_sweep
        if not isinstance(stream_list, core.MeasurementList):
            stream_list = core.MeasurementList(stream_list)
        self.stream_list = stream_list
        self.number = number
        super(SingleSweepStreamList, self).__init__(state=state, description=description)

    @property
    def resonator(self):
        return self.single_sweep.resonator

    def x_and_q(self, index):
        stream = self.stream_list[index]
        return self.resonator.invert_raw(frequency=stream.frequency, s21_raw=stream.s21_raw)

    def state_vector(self, *keys):
        vector = []
        for stream in self.stream_list:
            state = stream.state
            for key in keys:
                if state is np.nan:  # Not clear that this actually works
                    break
                state = state.get(key, np.nan)
            vector.append(state)
        return np.array(vector)


class Scan(RoachMeasurement):
    """
    This class contains a MeasurementList of SweepArrays, and allows them to be addressed as a single scan across the
    entire range of frequencies. It can handle overlap by averaging.
    """
    _version = 0

    def __init__(self, sweep_arrays, state=None, description=''):
        """
        Parameters
        ----------
        sweep_arrays : list (SweepArray)
        state : dict
        description : str
        """
        if not isinstance(sweep_arrays, core.MeasurementList):
            sweep_arrays = core.MeasurementList(sweep_arrays)
        self.sweep_arrays = sweep_arrays
        super(Scan, self).__init__(state=state, description=description)

    @property
    def frequency(self):
        return np.concatenate([sa.frequency for sa in self.sweep_arrays])

    @property
    def s21_point(self):
        return np.concatenate([sa.s21_point for sa in self.sweep_arrays])

    @property
    def s21_point_foreground(self):
        return np.concatenate([sa.s21_point_foreground for sa in self.sweep_arrays])

    # ToDo: test and finish
    def stitch(self, kernel=None):
        n = self.sweep_arrays[0].frequency.size
        duplicates = np.where(self.sweep_arrays[0].frequency == self.sweep_arrays[1].frequency[0])[0]
        if duplicates.size == 0:
            overlap = 0
        else:
            overlap = self.sweep_arrays[0].frequency.size - int(duplicates)  # will fail if more than 1 duplicate
        left_weight = np.linspace(1, 0, overlap)
        right_weight = np.linspace(0, 1, overlap)
        frequencies = [self.sweep_arrays[0].frequency[:overlap]]
        amplitudes = [np.abs(self.sweep_arrays[0].s21_point[:overlap])]
        for left, right in zip(self.sweep_arrays[:-1], self.sweep_arrays[1:]):
            frequencies.append(left.frequency[overlap:])
            amplitudes.append(left_weight * np.abs(left.s21_point[overlap:]) +
                              right_weight * np.abs(right.s21_point[:overlap]))
        frequencies.append(self.sweep_arrays[-1].frequency[overlap:])
        amplitudes.append(np.abs(self.sweep_arrays[-1].s21_point[overlap:]))
        frequency = np.concatenate(frequencies)
        amplitude = np.concatenate(amplitudes)
        if kernel is None:
            width = int(n / 10)
            kernel = np.exp(-np.linspace(-4, 4, width) ** 2)
            kernel /= kernel.sum()
        smoothed = np.convolve(kernel, amplitude, mode='same')
        smoothed[:width] = smoothed[width + 1]
        smoothed[-width:] = smoothed[-(width + 1)]
        return frequency, amplitude, smoothed
