import time

import numpy as np

from kid_readout.interactive import *
from kid_readout.equipment import hardware
from kid_readout.measurement import acquire
from kid_readout.roach import analog, hardware_tools, attenuator, r2heterodyne
from kid_readout.equipment import agilent_33220

fg = agilent_33220.FunctionGenerator(addr=('192.168.0.202', 5025))
fg.set_load_ohms(1000)
fg.set_dc_voltage(0)
fg.enable_output(False)

ifboard = analog.HeterodyneMarkII()

setup = hardware.Hardware(ifboard)

ri = hardware_tools.r2_with_mk2()
ri.initialize()
#setup = hardware.Hardware()
#ri = hardware_tools.r2h14_with_mk2(initialize=True, use_config=False)
ri.iq_delay=-1

ri.set_fft_gain(6)

initial_f0s = np.load('/data/readout/resonances/2018-05-04-medley-efield-2GHz.npy')/1e6

#ilo = 3200.
ilo = 2370.

ri.set_lo(ilo)

initial_lo = (ilo)

nf = len(initial_f0s)
atonce = 4
if nf % atonce > 0:
    print "extending list of resonators to make a multiple of ", atonce
    initial_f0s = np.concatenate((initial_f0s, np.arange(1, 1 + atonce - (nf % atonce)) + initial_f0s.max()))

print len(initial_f0s)
print initial_f0s

nsamp = 2**16
offsets = np.arange(-256,256)*512./nsamp

#voltages = [1.5, 2]
voltages = [0, .0005, .001, .0015, .002, .0025, .003, .004, .005, .01, .015, .02, .025, .05, .1, .25, .5, 1, 1.5, 2]

#voltages = [.15, .25, .5, 1, 1.5, 2]

for heater_voltage in voltages:
    fg.set_dc_voltage(heater_voltage)
    if heater_voltage == 0:
        print "voltage is 0 V, skipping wait"
    else:
        print "waiting 1 minutes", heater_voltage
        time.sleep(60)
    fg.enable_output(True)

    for (lo,f0s) in [(initial_lo,initial_f0s)]:
        ri.set_lo(lo)
        for dac_atten in [10]:
            ri.set_dac_atten(dac_atten)
            tic = time.time()
            ncf = new_nc_file(suffix='%d_dB_%.3f_V' % (dac_atten, heater_voltage))
            swpa = acquire.run_sweep(ri, tone_banks=f0s[None,:] + offsets[:,None], num_tone_samples=nsamp,
                                     length_seconds=0, state=setup.state(), verbose=True,
                                     description='dark sweep')
            print "resonance sweep done", (time.time()-tic)/60.
            ncf.write(swpa)
            current_f0s = []
            for sidx in range(f0s.shape[0]):
                swp = swpa.sweep(sidx)
                res = swp.resonator
                print res.f_0, res.Q, res.current_result.redchi, (f0s[sidx]*1e6-res.f_0)
                if np.abs(res.f_0 - f0s[sidx]*1e6) > 200e3:
                    current_f0s.append(f0s[sidx]*1e6)
                    print "using original frequency for ",f0s[sidx]
                else:
                    current_f0s.append(res.f_0)
            print "fits complete", (time.time()-tic)/60.
            current_f0s = np.array(current_f0s)/1e6
            current_f0s.sort()
            if np.any(np.diff(current_f0s)<0.031):
                print "problematic resonator collision:",current_f0s
                print "deltas:",np.diff(current_f0s)
                problems = np.flatnonzero(np.diff(current_f0s)<0.031)+1
                current_f0s[problems] = (current_f0s[problems-1] + current_f0s[problems+1])/2.0
            if np.any(np.diff(current_f0s)<0.031):
                print "repeated problematic resonator collision:",current_f0s
                print "deltas:",np.diff(current_f0s)
                problems = np.flatnonzero(np.diff(current_f0s)<0.031)+1
                current_f0s[problems] = (current_f0s[problems-1] + current_f0s[problems+1])/2.0
            ri.set_tone_freqs(current_f0s,nsamp)
            ri.select_fft_bins(range(f0s.shape[0]))
            #raw_input("turn off compressor")
            meas = ri.get_measurement(num_seconds=60., state=setup.state(),description='source off stream')
            ncf.write(meas)
            print "dac_atten %f done in %.1f minutes" % (dac_atten, (time.time()-tic)/60.)
            ncf.close()

fg.set_dc_voltage(0)
ri.set_dac_atten(20)
