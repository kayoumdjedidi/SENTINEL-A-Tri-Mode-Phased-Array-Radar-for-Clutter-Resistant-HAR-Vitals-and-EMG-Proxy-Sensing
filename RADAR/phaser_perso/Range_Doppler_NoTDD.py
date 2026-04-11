'''FMCW Range-Doppler (continuous sawtooth, no TDD)
   Network-safe version for WSL -> Pi -> Pluto iiod chain.
   Replaces TDD single_sawtooth_burst with continuous_sawtooth:
     - One large rx() call grabs num_chirps frames in one contiguous buffer
     - No burst trigger, no timing races over the network
   Based on Range_Doppler_Plot.py by Jon Kraft (Sept 2024)
'''

import sys
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
plt.close('all')

import adi
print(adi.__version__)

# --------------------------------------------------------------------------
# Key Parameters
# --------------------------------------------------------------------------
sample_rate  = 4e6
center_freq  = 2.1e9
signal_freq  = 100e3
rx_gain      = 60       # dB, -3 to 70
tx_gain      = 0        # dB, 0 to -88 (0 = max)
output_freq  = 10.25e9    # desired RF output
chirp_BW     = 500e6
ramp_time_us = 500      # us — with delay_word=0 gives exactly 2000 samples at 4 MHz
num_chirps   = 64
max_range    = 100      # m, y-axis limit
min_scale    = 0        # log10 colormap floor
max_scale    = 8        # log10 colormap ceiling
mti_filter   = True     # 2-pulse canceller MTI
save_data    = True    # set True to save raw bursts for offline reprocessing
save_file    = "notdd_4MSPS_500M_500u_64.npy"   # data filename; _config.npy saved alongside

# --------------------------------------------------------------------------
# Hardware instantiation
# --------------------------------------------------------------------------
rpi_ip = "ip:phaser.local"
sdr_ip = "ip:phaser.local:50901"

my_sdr = adi.ad9361(uri=sdr_ip)
my_sdr._ctx.set_timeout(0)   # infinite timeout for large buffers
my_phaser = adi.CN0566(uri=rpi_ip, sdr=my_sdr)

# Phaser array init
my_phaser.configure(device_mode="rx")
my_phaser.element_spacing = 0.014
my_phaser.load_gain_cal()
my_phaser.load_phase_cal()
for i in range(8):
    my_phaser.set_chan_phase(i, 0)

gain_list = [127] * 8   # uniform taper; swap for [8,34,84,127,127,84,34,8] Blackman
for i in range(len(gain_list)):
    my_phaser.set_chan_gain(i, gain_list[i], apply_cal=True)

my_phaser._gpios.gpio_tx_sw   = 0   # TX_OUT_2
my_phaser._gpios.gpio_vctrl_1 = 1   # use onboard PLL/LO
my_phaser._gpios.gpio_vctrl_2 = 1   # enable TX path

# --------------------------------------------------------------------------
# SDR config
# --------------------------------------------------------------------------
my_sdr.sample_rate = int(sample_rate)
my_sdr.rx_lo = int(center_freq)
my_sdr.rx_enabled_channels = [0, 1]
my_sdr.gain_control_mode_chan0 = 'manual'
my_sdr.gain_control_mode_chan1 = 'manual'
my_sdr.rx_hardwaregain_chan0 = int(rx_gain)
my_sdr.rx_hardwaregain_chan1 = int(rx_gain)

my_sdr.tx_lo = int(center_freq)
my_sdr.tx_enabled_channels = [0, 1]
my_sdr.tx_cyclic_buffer = True
my_sdr.tx_hardwaregain_chan0 = -88           # TX0 off (loopback isolation)
my_sdr.tx_hardwaregain_chan1 = int(tx_gain)  # TX1 active

# --------------------------------------------------------------------------
# ADF4159 ramp config — continuous sawtooth, no TDD trigger
# delay_word = 0 -> period = ramp_time_us exactly -> N_frame = 2000 samples
# --------------------------------------------------------------------------
vco_freq  = int(output_freq + signal_freq + center_freq)
BW        = chirp_BW
num_steps = int(ramp_time_us)   # 1 step per us is stable
my_phaser.frequency      = int(vco_freq / 4)
my_phaser.freq_dev_range = int(BW / 4)
my_phaser.freq_dev_step  = int((BW / 4) / num_steps)
my_phaser.freq_dev_time  = int(ramp_time_us)
my_phaser.delay_word     = 0        # no end-of-ramp hold -> period = freq_dev_time
my_phaser.delay_clk      = "PFD"
my_phaser.delay_start_en = 0
my_phaser.ramp_delay_en  = 0
my_phaser.trig_delay_en  = 0
my_phaser.ramp_mode      = "continuous_sawtooth"  # free-running, no burst sync needed
my_phaser.sing_ful_tri   = 0
my_phaser.tx_trig_en     = 0   # no trigger — ramp runs continuously
my_phaser.enable         = 0   # write last to latch all registers

# Confirm actual ramp time programmed
ramp_time_us = int(my_phaser.freq_dev_time)
ramp_time_s  = ramp_time_us / 1e6
print(f"ramp_time = {ramp_time_us} us")

# --------------------------------------------------------------------------
# Buffer sizing
# N_frame: samples per chirp period (exact when delay_word=0)
# We grab num_chirps+1 frames and skip the first (may be mid-chirp on first call)
# --------------------------------------------------------------------------
N_frame = int(ramp_time_s * sample_rate)   # 2000 samples
print(f"N_frame = {N_frame} samples/chirp")

total_samples = (num_chirps + 1) * N_frame
power = int(np.ceil(np.log2(total_samples)))
buffer_size = int(2 ** power)              # 2^17 = 131072 (covers 65.5 chirps)
my_sdr.rx_buffer_size = buffer_size
buffer_time_ms = buffer_size / sample_rate * 1000
print(f"buffer_size = {buffer_size} ({buffer_time_ms:.1f} ms)")

# --------------------------------------------------------------------------
# Range / velocity axes
# --------------------------------------------------------------------------
c          = 3e8
wavelength = c / output_freq
slope      = BW / ramp_time_s
PRI_s      = ramp_time_s
PRF        = 1 / PRI_s
R_res      = c / (2 * BW)
v_res      = wavelength / (2 * num_chirps * PRI_s)
max_doppler_vel = (PRF / 2) * wavelength / 2

# Range-FFT axis: N_frame bins centered on 0
freq = np.linspace(-sample_rate / 2, sample_rate / 2, N_frame)
dist = (freq - signal_freq) * c / (2 * slope)

print(f"Range res: {R_res:.3f} m | Vel res: {v_res:.3f} m/s | Max vel: ±{max_doppler_vel:.1f} m/s")

# --------------------------------------------------------------------------
# TX sinewave (cyclic)
# --------------------------------------------------------------------------
N_tx = int(2 ** 18)
fc   = int(signal_freq)
ts   = 1 / float(sample_rate)
t    = np.arange(0, N_tx * ts, ts)
i_tx = np.cos(2 * np.pi * t * fc) * 2 ** 14
q_tx = np.sin(2 * np.pi * t * fc) * 2 ** 14
iq   = 0.9 * (i_tx + 1j * q_tx)
try:
    my_sdr.tx_destroy_buffer()   # clear any buffer left open by a previous run
except Exception:
    pass
my_sdr.tx([iq, iq])

# --------------------------------------------------------------------------
# Data acquisition
# --------------------------------------------------------------------------
def get_radar_data():
    '''Single rx() call -> segment into num_chirps frames.
    Skip the first frame (may start mid-chirp on first buffer fill).'''
    data = my_sdr.rx()
    sum_data = data[0] + data[1]
    rx_bursts = np.zeros((num_chirps, N_frame), dtype=complex)
    offset = N_frame   # skip first frame
    for burst in range(num_chirps):
        start = offset + burst * N_frame
        rx_bursts[burst] = sum_data[start : start + N_frame]
    return rx_bursts

def apply_mti(rx_bursts):
    '''2-pulse canceller: removes static clutter (zero-Doppler) chirp by chirp.'''
    Chirp2P = np.zeros_like(rx_bursts)
    for chirp in range(num_chirps - 1):
        c0   = rx_bursts[chirp]
        c1   = rx_bursts[chirp + 1]
        corr = np.correlate(c0, c1, 'valid')
        phi  = np.angle(corr[0])
        Chirp2P[chirp] = c1 - c0 * np.exp(-1j * phi)
    return Chirp2P

def freq_process(data):
    '''2D FFT -> range-Doppler map, log magnitude.
    Input:  (num_chirps, N_frame) complex
    Output: (N_frame, num_chirps) float for imshow'''
    rd = np.fft.fftshift(np.abs(np.fft.fft2(data)))
    rd = np.log10(rd + 1e-10).T
    return np.clip(rd, min_scale, max_scale)

# --------------------------------------------------------------------------
# Initial capture + plot setup
# --------------------------------------------------------------------------
rx_bursts  = get_radar_data()
if mti_filter:
    rx_bursts = apply_mti(rx_bursts)
radar_data = freq_process(rx_bursts)

extent = [-max_doppler_vel, max_doppler_vel, dist.min(), dist.max()]

fig, ax = plt.subplots(figsize=(14, 7))
try:
    img = ax.imshow(radar_data, aspect='auto', extent=extent, origin='lower',
                    cmap=matplotlib.colormaps.get_cmap('inferno'))
except Exception:
    from matplotlib.cm import get_cmap
    img = ax.imshow(radar_data, aspect='auto', extent=extent, origin='lower',
                    cmap=get_cmap('inferno'))

ax.set_title('Range-Doppler Spectrum (continuous sawtooth, no TDD)', fontsize=18)
ax.set_xlabel('Velocity [m/s]', fontsize=14)
ax.set_ylabel('Range [m]', fontsize=14)
ax.set_xlim([-10, 10])
ax.set_ylim([0, max_range])
ax.set_yticks(np.arange(0, max_range, max_range / 20))
plt.tight_layout()

print(f"sample_rate={sample_rate/1e6:.0f} MHz  ramp={ramp_time_us} us  "
      f"num_chirps={num_chirps}  MTI={'on' if mti_filter else 'off'}  "
      f"save={'on -> '+save_file if save_data else 'off'}")
print("CTRL+C to stop")
all_captures = []

# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
try:
    while True:
        rx_bursts = get_radar_data()
        if save_data:
            all_captures.append(rx_bursts.copy())
        if mti_filter:
            rx_bursts = apply_mti(rx_bursts)
        radar_data = freq_process(rx_bursts)
        img.set_data(radar_data)
        plt.pause(0.05)
except KeyboardInterrupt:
    pass

# --------------------------------------------------------------------------
# Cleanup + optional save
# --------------------------------------------------------------------------
my_sdr.tx_destroy_buffer()
print("Pluto TX buffer cleared.")

if save_data and all_captures:
    np.save(save_file, all_captures)
    cfg = [sample_rate, signal_freq, output_freq, num_chirps,
           chirp_BW, ramp_time_s, ramp_time_us / 1e3]   # frame_length_ms = ramp_time_us/1e3
    np.save(save_file[:-4] + "_config.npy", cfg)
    print(f"Saved {len(all_captures)} frames -> {save_file}")
    print(f"Config -> {save_file[:-4]}_config.npy")
