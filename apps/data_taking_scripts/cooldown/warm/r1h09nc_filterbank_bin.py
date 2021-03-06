"""
Sweep one tone across its filter bank bin.
"""
import time

import numpy as np

from kid_readout.roach import analog, hardware_tools, tools
from kid_readout.measurement import acquire
from kid_readout.equipment import hardware

acquire.show_settings()
acquire.show_git_status()
logger = acquire.get_script_logger(__file__)

# Parameters
suffix = 'filterbank_bin'
lo_MHz = 3000
baseband_MHz = 100
lo_round_to_MHz = 2.5e-3
dac_attenuation = 10
tones_per_bin_exponent = 5
half_width_in_bins = 3
stream_length_blocks = 1
wait = 5

# Hardware
conditioner = analog.HeterodyneMarkII()
hw = hardware.Hardware(conditioner)
ri = hardware_tools.r1h09nc_with_mk2(initialize=True, use_config=False)

# Calculate tone bin integers
f_filterbank_MHz = ri.fs / ri.nfft
n_filterbank = int(np.round(baseband_MHz / f_filterbank_MHz))
tone_sample_exponent = int(np.log2(ri.nfft) + tones_per_bin_exponent)
center_integer = 2 ** tones_per_bin_exponent * n_filterbank
tone_integers = center_integer + np.arange(-half_width_in_bins * 2 ** tones_per_bin_exponent,
                                           half_width_in_bins * 2 ** tones_per_bin_exponent + 1)

# Acquire
npd = acquire.new_npy_directory(suffix=suffix)
tic = time.time()
try:
    tools.set_and_attempt_external_phase_lock(ri, f_lo=lo_MHz, f_lo_spacing=lo_round_to_MHz)
    ri.set_dac_attenuator(dac_attenuation)
    ri.set_tone_bins(bins=np.array([center_integer]), nsamp=2 ** tone_sample_exponent)
    ri.fft_bins = np.atleast_2d(np.array([n_filterbank]))
    ri.select_bank(0)
    ri.select_fft_bins(np.array([0]))
    time.sleep(wait)
    tools.optimize_fft_gain(ri)
    for tone_integer in tone_integers:
        ri.set_tone_bins(bins=np.array([tone_integer]), nsamp=2 ** tone_sample_exponent)
        ri.fft_bins = np.atleast_2d(np.array([n_filterbank]))
        ri.select_bank(0)
        ri.select_fft_bins(np.array([0]))
        time.sleep(wait)
        npd.write(ri.get_measurement_blocks(num_blocks=stream_length_blocks, demod=False, state=hw.state()))
        npd.write(ri.get_adc_measurement())
finally:
    npd.close()
    print("Wrote {}".format(npd.root_path))
    print("Elapsed time {:.0f} minutes.".format((time.time() - tic) / 60))
