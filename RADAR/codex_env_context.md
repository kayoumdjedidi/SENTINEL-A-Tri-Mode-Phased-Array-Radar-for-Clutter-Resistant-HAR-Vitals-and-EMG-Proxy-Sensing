24 previous messages
I made a new script: sentinel_modeA_v4.py.

What changed:

added chirp-to-chirp static phase deskew before RD formation
switched leakage blanking from theoretical bin math to measured near-range Doppler column detection
fixed background subtraction to work in linear magnitude instead of subtracting log maps
changed auto-gating to use the beat-frequency range profile instead of the corrupted RD image
kept it as a separate script so your current versions stay untouched
Run it with:

python3 /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_v4.py
In the UI, the important defaults are already on:

Deskew static phase to DC
Blank measured leakage column
Hanning window
py_compile passed. I did not hardware-test it here, so the real check is whether the fixed stripe collapses and whether Capture BG stops turning the display fully black. If it still fails, the next step is not another UI tweak; it’s logging one short raw session from v4 and inspecting the chirp matrix offline.





7:08 PM
7 files changed
+157
-89
Undo

Review
workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_v4.py
workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_v4.py
workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_v4.py
workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_v4.py
workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_v4.py
workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_v4.py
workspace/session_log.md
nope nothing is better, you can go back to the drawing board and reason from first pricicples by using the refence stuff  /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/reference/Range_Doppler_Plot.py
/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/reference/Range_Doppler_Processing.py
/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/reference/video_transcript.md
/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/reference/target_detection_dbfs.py
7:13 PM


10 previous messages
Yes. The first-principles difference is chirp synchrony.

Range_Doppler_Plot.py assumes:

TDD-synchronised burst capture
explicit chirp slicing from one contiguous buffer
plain 2D FFT
optional 2-pulse MTI only
Our v2/v3/v4 experiments kept trying to recover that from continuous capture plus extra cleanup. If the chirp columns are wrong, everything after that is garbage.

I added a new script that stays close to the reference path:
sentinel_modeA_ref_tdd.py

What it does:

TDD burst sync like reference/Range_Doppler_Plot.py
slices bursts using start_offset_samples + burst * N_frame
optional 2-pulse MTI
plain fixed-scale range-Doppler plot
optional raw save compatible with Range_Doppler_Processing.py
Run this first with direct Pluto if at all possible:

python3 /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py --sdr-uri ip:192.168.2.1
Then try MTI:

python3 /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py --sdr-uri ip:192.168.2.1 --mti
If you want a saved burst for offline reference processing:

python3 /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py --sdr-uri ip:192.168.2.1 --save-data --save-path /tmp/ref_capture.npy
py_compile passed.

If this script also gives bad RD, the problem is lower-level than our UI/DSP tweaks:

TDD transport path
burst timing/sync
hardware signal chain
or actual RF visibility/SNR
If it works, then we know the continuous-mode SENTINEL stack is the thing that needs rework, not the radar itself.





7:16 PM
2 files changed
+350
-0
Undo

Review
workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py
workspace/session_log.md
ode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ python3 sentinel_modeA_ref_tdd.py 
0.0.20
Traceback (most recent call last):
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/adi/context_manager.py", line 36, in __init__
    self._ctx = iio.Context(self.uri)
                ~~~~~~~~~~~^^^^^^^^^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/iio.py", line 1328, in __init__
    self._context = _new_uri(_context.encode("ascii"))
                    ~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/iio.py", line 56, in _check_null
    raise OSError(err, _strerror(err))
TimeoutError: [Errno 110] Connection timed out

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py", line 349, in <module>
    sys.exit(main())
             ~~~~^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py", line 157, in main
    my_sdr = adi.ad9361(uri=args.sdr_uri)
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/adi/rx_tx.py", line 734, in __init__
    rx_def.__init__(self, *args, **kwargs)
    ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/adi/rx_tx.py", line 653, in __init__
    shared_def.__init__(self, *args, **kwargs)
    ~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/adi/rx_tx.py", line 603, in __init__
    context_manager.__init__(self, uri_ctx, self._device_name)
    ~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/adi/context_manager.py", line 38, in __init__
    raise Exception("No device found")
Exception: No device found
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ 

Show more
7:18 PM


4 previous messages
That error is not a radar-processing failure. It dies at the first device open.

Meaning:

the script tried Pluto at ip:192.168.2.1
your current environment cannot reach Pluto directly
so we never even got to TDD, chirp slicing, or RD math
I patched the reference script so it now:

tries direct Pluto first
then falls back to ip:phaser.local:50901
prints clearly which path it actually used
Rerun:

python3 /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py
What to expect:

If it says Pluto URI: ip:192.168.2.1, good: that is the true reference path.
If it says Pluto URI: ip:phaser.local:50901, we are in forwarded mode.
If forwarded mode then later fails at rx(), that is strong evidence the TDD burst transport is the blocker, not the DSP.
If you want to force the forwarded path explicitly:

python3 sentinel_modeA_ref_tdd.py --sdr-uri ip:phaser.local:50901
If you want to force the direct path explicitly:

python3 sentinel_modeA_ref_tdd.py --sdr-uri ip:192.168.2.1
py_compile passed after the patch.

The key first-principles conclusion has not changed:

if direct TDD works, our continuous SENTINEL stack is the problem
if direct TDD is unreachable and forwarded TDD times out, the transport path is the problem
only after that do we revisit RD math again
Send me the next console output after rerunning this patched script.





7:20 PM
1 file changed
Undo

Review
workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py
ode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ python3 sentinel_modeA_ref_tdd.py 
0.0.20
[ref] Pluto URI: ip:phaser.local:50901
[ref] WARNING: using the forwarded Pluto path. If TDD later times out at rx(), that is a transport limitation, not a range-Doppler math issue.
file not found, loading default (all gain at maximum)
file not found, loading default (no phase shift)
[ref] rf=10.2500 GHz  bw=500 MHz  ramp=300 us  chirps=256
[ref] PRI=0.500 ms  N_frame=2000  good_ramp_samples=1079  start_offset_samples=119
[ref] rx_buffer_size=524288  buffer_time=131.1 ms  R_res=0.300 m  v_res=0.114 m/s
Traceback (most recent call last):
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py", line 368, in <module>
    sys.exit(main())
             ~~~~^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py", line 281, in main
    raw = get_radar_data(
        my_sdr=my_sdr,
    ...<4 lines>...
        good_ramp_samples=good_ramp_samples,
    )
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py", line 139, in get_radar_data
    data = my_sdr.rx()
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/adi/rx_tx.py", line 275, in rx
    data = self.__rx_complex()
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/adi/rx_tx.py", line 240, in __rx_complex
    x = self._rx_buffered_data()
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/adi/compat.py", line 149, in _rx_buffered_data
    self._rxbuf.refill()
    ~~~~~~~~~~~~~~~~~~^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/iio.py", line 1003, in refill
    _buffer_refill(self._buffer)
    ~~~~~~~~~~~~~~^^^^^^^^^^^^^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/.venv/lib/python3.13/site-packages/iio.py", line 62, in _check_negative
    raise OSError(-result, _strerror(-result))
TimeoutError: [Errno 110] Connection timed out
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ 

Show more
7:20 PM


16 previous messages
The TDD reference result gives us the answer: over phaser.local:50901, the reference burst path dies at my_sdr.rx(). So on this machine, the real problem is not “wrong RD math in the reference.” It is: you do not have the transport needed for the reference TDD acquisition model.

So I made the next honest script:
sentinel_modeA_ref_contsync.py

What it does differently:

uses continuous_sawtooth
grabs a raw continuous buffer
searches for the best chirp offset and samples-per-chirp each frame
slices chirps from that estimated alignment
then does the plain reference-style 2D FFT
optional 2-pulse MTI, nothing fancier
Run it first without MTI:

python3 /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_contsync.py
Then try with MTI:

python3 /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_contsync.py --mti
Watch the console:

spc
offset
score
If those are stable frame to frame and the image is still dead, then we are down to actual RF visibility / SNR, not segmentation.

py_compile passed.

At this point the logic tree is:

reference TDD on forwarded path: transport-limited
new continuous-sync search script: tests whether unsynchronised segmentation was the real culprit
if this also fails, the remaining suspects are hardware path, coupling, gain, or scene SNR
Send me the console output and screenshot from sentinel_modeA_ref_contsync.py.





7:23 PM
2 files changed
+401
-0
Undo

Review
workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_contsync.py
workspace/session_log.md
but we dont have to stick to this enviormenem, if TDD is truly crutial to making things work, i can go back to running stuff on the raspi durectly instead of like we are doing now with streaming to my pc, but maybe that will give us worse peformance, or maybe we could split things where the TDD would happen on the Pi live and the rest is on my pc that way it works, lets think first
7:25 PM


13 previous messages
No, we should not stay married to the current environment.

The repo already points to the answer:

OPERATIONS.md (line 174) says TDD should run either on the Pi via direct Pluto access or from WSL only if 192.168.2.1 is reachable directly.
radar_backends.py (line 232) explicitly says burst TDD over phaser.local:50901 is transport-limited and reliable with direct Pluto or when running on the Pi.
offline_replay.py (line 1) already gives us the clean replay path on the PC.
My view:

If the goal is a trustworthy Mode A baseline, TDD or equally hard chirp synchrony is probably crucial right now.
Running acquisition on the Pi will likely improve capture correctness, not hurt it.
The Pi may be worse for heavy GUI work, but that is a separate problem from acquisition correctness.
Best architecture:

Pi owns hardware control and TDD burst capture.
Pi saves raw bursts plus config locally, or computes a compact RD output.
PC does replay, plotting, analysis, and experiments.
So yes, the split you suggested is the right one:

Pi for acquisition and chirp slicing
PC for display and higher-level DSP
not PC pulling raw TDD bursts live over the forwarded Pluto context
I would avoid this:

WSL/PC doing my_sdr.rx() for raw TDD bursts through phaser.local:50901
I would recommend this order:

Prove the reference TDD path on the Pi directly.
Save one or two known-good captures.
Replay them on the PC and verify the RD/DSP stack.
Only then add a live Pi-to-PC stream, preferably of compact products like RD maps, range profiles, micro-Doppler rows, and diagnostics rather than raw Pluto buffers.
So the answer is: yes, move TDD acquisition back to the Pi if you want the shortest path to something real. The likely end-state is hybrid, not all-on-Pi and not all-on-PC.

If you want, I can next write a very concrete split plan:

what runs on Pi
what runs on PC
what data gets saved/streamed
the smallest code changes to get there fast




7:28 PM
1 file changed
Undo

Review
workspace/session_log.md
what if we make this Best architecture:

Pi owns hardware control and TDD burst capture.
Pi saves raw bursts plus config locally, or computes a compact RD output.
PC does replay, plotting, analysis, and experiments.

But instead of just saving and then reviwing ( so much wasted time in exerimenting and debugging) we manage to stream it all always to the pc, and the pc handles the heavy GUI stuff
or split it in the best way possible where the PI only does waht it needs to, and the PC hanldes anything else that's heavier.

and are we really sure that we cant solve this with the current setup, maybe going over serial instead of ethernet, or some oher way
7:31 PM


2 previous messages
Yes, streaming live from the Pi to the PC is the right direction if direct PC-to-Pluto TDD is unavailable.

What we know from the repo:

The current failure is specifically the forwarded Pluto path over phaser.local:50901, not “Python is too slow” or “the GUI is too heavy.” radar_backends.py (line 232)
The repo already says TDD is reliable with direct Pluto access or when running on the Pi. radar_backends.py (line 238) OPERATIONS.md (line 174)
There is already an attempted reduced-buffer TDD profile for the current WSL->Pi->Pluto path, so this path has not been ignored. sentinel_mode_a.py (line 111)
My recommendation is a two-stage decision tree:

Best-case, still mostly keep your current workflow:
Keep GUI/DSP on the PC.
But stop using forwarded ip:phaser.local:50901 for Pluto.
Test direct Pluto access from the PC with ip:192.168.2.1.
If that works, PC can own TDD and GUI, while Pi still handles CN0566 control.
This is the simplest fix.
If direct Pluto from the PC is not possible:
Pi owns TDD capture.
PC owns GUI and most experimentation.
Stream live from Pi to PC.
The split I would actually build:

Pi:
program CN0566 + Pluto
run TDD burst capture locally
slice to chirp_matrix
maintain a rolling ring buffer of recent raw frames
expose controls: start/stop, beam angle, gains, record snapshot
PC:
receive live frames
run MTI / RD / CFAR / gating / display
do replay, parameter experiments, and debugging UI
The key design point:

Do not stream the fragile raw Pluto DMA transaction itself.
Let the Pi do the hard real-time step.
Stream either:
chirp_matrix for maximum debugging flexibility
or RD/range-profile/micro-Doppler products for lower bandwidth
Best hybrid:
stream processed products continuously
stream chirp_matrix in debug mode
allow the PC to request the last N raw frames from the Pi ring buffer on demand
On serial:

UART/serial is not a serious transport for SDR burst data.
It is fine for control or logs.
It is not fine for live FMCW sample transport.
The credible alternatives are:
direct Pluto USB/Ethernet to PC
Pi-side capture plus Ethernet streaming of lighter products
So the real answer is:

we are not sure all PC-first solutions are impossible
but we are pretty sure the forwarded 50901 burst path is the wrong thing to keep betting on
the only current-setup variant worth testing hard is direct PC->Pluto
otherwise, Pi-capture + PC-visualization is the best architecture
If you want, I can turn this into a concrete implementation spec for:

pi_capture_daemon.py
pc_live_viewer.py
the frame format and control protocol
what stays on Pi vs what stays on PC




7:32 PM
okay give me what to do to test this all out 
7:34 PM

Use this order. It will tell us very quickly which architecture is worth building.

Scripts involved:

sentinel_mode_a.py
sentinel_modeA_ref_tdd.py
offline_replay.py
Test Order

Go to the repo:
cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
Best remaining test of the current forwarded setup:
python3 sentinel_mode_a.py --acq tdd --tdd-profile auto --frames 20
Pass = no ETIMEDOUT, frames render, no rx() timeout.
Fail = forwarded 50901 TDD is not worth more time.

Check whether the PC can talk to Pluto directly:
iio_info -u ip:192.168.2.1 | head -5
Pass = direct Pluto from the PC is possible.
Fail = PC cannot own TDD directly.

If step 3 passes, run the plain reference TDD path from the PC:
python3 sentinel_modeA_ref_tdd.py --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local
Pass = this is the simplest architecture: PC GUI + direct Pluto + Pi CN0566 control.

If step 3 fails, run the same reference TDD path on the Pi itself:
ssh pi@phaser.local
cd <repo>/RADAR/phaser_perso
python3 sentinel_modeA_ref_tdd.py --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --save-data --save-path /tmp/ref_capture.npy
Pass = Pi-side TDD acquisition is good.

Pull that capture back to the PC and replay it:
scp pi@phaser.local:/tmp/ref_capture.npy .
scp pi@phaser.local:/tmp/ref_capture_config.npy .
python3 offline_replay.py ref_capture.npy
Pass = acquisition is solved, and the PC-side DSP/plot path is good.

Decision

If step 2 passes: keep current setup.
If step 4 passes: use PC direct-to-Pluto.
If steps 5 and 6 pass: build Pi-capture + PC-display streaming.
If step 5 fails too: the problem is below transport, in hardware bring-up or RF chain.
What Not To Test

Do not spend time on serial/UART for live radar sample transport.
Serial is fine for control/logging, not for TDD burst data.
What To Send Me Back

Result of steps 2, 3, 5, and 6
Exact console error if any
One screenshot only for the first passing live plot
If you want, after you run these, I’ll give you the exact next implementation plan for the winning architecture.





7:36 PM
thats the worng ssh, the right one is phaser login as: analog
analog@phaser's password: analog  
Linux phaser 5.10.63-v7l+ #1 SMP Fri Mar 10 16:35:58 UTC 2023 armv7l

The programs included with the Debian GNU/Linux system are free software;
the exact distribution terms for each program are described in the
individual files in /usr/share/doc/*/copyright.

Debian GNU/Linux comes with ABSOLUTELY NO WARRANTY, to the extent
permitted by applicable law.
Last login: Sun Nov 23 13:13:20 2025
analog@phaser:~ 

and we have to copy the file there first ofc
ADAR/phaser_perso$ ssh pi@phaser.local
cd <repo>/RADAR/phaser_perso
python3 sentinel_modeA_ref_tdd.py --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --save-data --save-path /tmp/ref_capture.npy
pi@phaser.local's password: 
Permission denied, please try again.
pi@phaser.local's password: 

bash: repo: No such file or directory
0.0.20
Traceback (most recent call last):
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py", line 368, in <module>
    sys.exit(main())
             ~~~~^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py", line 168, in main
    my_sdr, sdr_uri = open_pluto(adi, args.sdr_uri)
                      ~~~~~~~~~~^^^^^^^^^^^^^^^^^^^
  File "/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py", line 103, in open_pluto
    raise RuntimeError(
        "Could not open Pluto on any candidate URI.\n" + "\n".join(errors)
    )
RuntimeError: Could not open Pluto on any candidate URI.
ip:192.168.2.1: No device found
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ ssh analog@phaser.local 
Linux phaser 5.10.63-v7l+ #1 SMP Fri Mar 10 16:35:58 UTC 2023 armv7l

The programs included with the Debian GNU/Linux system are free software;
the exact distribution terms for each program are described in the
individual files in /usr/share/doc/*/copyright.

Debian GNU/Linux comes with ABSOLUTELY NO WARRANTY, to the extent
permitted by applicable law.
Last login: Sun Nov 23 13:22:20 2025 from fe80::fa65:2a0b:496d:8e57%eth0
analog@phaser:~ $ ls
89-pluto.rules  hats               phaser_sdcard_setup.sh  radar_backends.py
Bookshelf       hostname           Pictures                radar_dsp.py
config.txt      hosts              plutosdr-fw-v0.38.zip   radar_mode_a.py
copyright       iiod-usb@.service  pluto_update_ad9361.sh  sentinel_radar.py
Desktop         img                Public                  target_detection_dbfs.py
Documents       LICENSE.html       pyadi-iio               Templates
Downloads       Music              __pycache__             Videos
analog@phaser:~ $ 

Downloads       Music              __pycache__             Videos
analog@phaser:~ $ iio_info -u ip:192.168.2.1 | head -5
Library version: 0.24 (git tag: accb7b5)
Compiled with backends: local xml ip usb serial
IIO context created with network backend.
Backend version: 0.26 (git tag: v0.26)
Backend description string: 192.168.2.1 Linux (none) 6.1.0-gf3da30df6004 #5 SMP PREEMPT Tue Oct 15 10:50:00 CEST 2024 armv7l
analog@phaser:~ $ 







Show more
7:40 PM


4 previous messages
Correct. Use analog@phaser.local, and copy the script first.

Your latest result is already useful:

PC -> ip:192.168.2.1 failed
Pi -> iio_info -u ip:192.168.2.1 worked
That means the next real test is Pi-local TDD.

Do This

From your PC, in phaser_perso:

cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp sentinel_modeA_ref_tdd.py analog@phaser.local:~/
Then SSH in:

ssh analog@phaser.local
On the Pi, first verify Python has what the script needs:

python3 -c "import adi, numpy, matplotlib; print(adi.__version__)"
If that works, run the reference script headless:

MPLBACKEND=Agg python3 ~/sentinel_modeA_ref_tdd.py \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --save-data \
  --save-path /tmp/ref_capture.npy
Let it run for 5 to 10 seconds, then press Ctrl+C.

If it worked, you should see:

[ref] Pluto URI: ip:192.168.2.1
[ref] CTRL+C to stop
after Ctrl+C: [ref] Saved ... bursts -> /tmp/ref_capture.npy
Then exit the Pi:

exit
Back on your PC, copy the capture back:

cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp analog@phaser.local:/tmp/ref_capture.npy .
scp analog@phaser.local:/tmp/ref_capture_config.npy .
Replay it on the PC:

python3 offline_replay.py ref_capture.npy
How To Interpret It

If Pi-side sentinel_modeA_ref_tdd.py works and replay looks sane:
TDD acquisition is fine
forwarded 50901 is the bottleneck
the right architecture is Pi capture + PC visualization/analysis
If Pi-side direct TDD still fails:
the issue is deeper than forwarding
then we inspect Pi-side TDD config / hardware chain next
Important
Your earlier ssh pi@phaser.local test was invalid for two reasons:

wrong user
the script actually ended up running on your PC after the SSH failure
Send Me Back

output of:
python3 -c "import adi, numpy, matplotlib; print(adi.__version__)"
first 15 to 20 lines from the Pi run
whether /tmp/ref_capture.npy got created
result of python3 offline_replay.py ref_capture.npy
If you want, after that I’ll give you the exact minimal live Pi->PC streaming architecture to implement next.





7:41 PM
i waited 20 seconds
nalog@phaser:~ $ exit
logout
Connection to phaser.local closed.
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp sentinel_modeA_ref_tdd.py analog@phaser.local:~/
sentinel_modeA_ref_tdd.py                                     100%   13KB   2.7MB/s   00:00    
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ ssh analog@phaser.local
Linux phaser 5.10.63-v7l+ #1 SMP Fri Mar 10 16:35:58 UTC 2023 armv7l

The programs included with the Debian GNU/Linux system are free software;
the exact distribution terms for each program are described in the
individual files in /usr/share/doc/*/copyright.

Debian GNU/Linux comes with ABSOLUTELY NO WARRANTY, to the extent
permitted by applicable law.
Last login: Sun Nov 23 13:23:00 2025 from 192.168.1.100
analog@phaser:~ $ python3 -c "import adi, numpy, matplotlib; print(adi.__version__)"
0.0.20
analog@phaser:~ $ MPLBACKEND=Agg python3 ~/sentinel_modeA_ref_tdd.py \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --save-data \
  --save-path /tmp/ref_capture.npy
0.0.20
^C
^C^C
^Z
[1]+  Stopped                 MPLBACKEND=Agg python3 ~/sentinel_modeA_ref_tdd.py --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --save-data --save-path /tmp/ref_capture.npy
analog@phaser:~ $ exit
logout
There are stopped jobs.
analog@phaser:~ $ exit
logout
Connection to phaser.local closed.
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp analog@phaser.local:/tmp/ref_capture.npy .
scp analog@phaser.local:/tmp/ref_capture_config.npy .
scp: /tmp/ref_capture.npy: No such file or directory
scp: /tmp/ref_capture_config.npy: No such file or directory
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ 

Show more
7:43 PM


6 previous messages
This narrows it down.

Because the Pi run printed only 0.0.20 and never printed [ref] Pluto URI: ..., the hang is earlier than capture and plotting. It is inside the Pluto open path, most likely adi.ad9361(uri='ip:192.168.2.1').

Also, no file was saved because ^Z stopped the process before it reached the script’s cleanup block.

I patched sentinel_modeA_ref_tdd.py to add:

explicit stage prints
--frames
--no-plot
Use this exact sequence.

1. Copy the patched script again

cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp sentinel_modeA_ref_tdd.py analog@phaser.local:~/
2. SSH in and clear any stale stopped copy

ssh analog@phaser.local
pkill -f sentinel_modeA_ref_tdd.py || true
3. Test the exact Pluto open by itself

timeout 15s python3 -u -c "import adi; print('before ad9361', flush=True); s=adi.ad9361(uri='ip:192.168.2.1'); print('after ad9361', flush=True)"
echo $?
Interpretation:

prints after ad9361 and exit code 0 = Pluto open is fine
exit code 124 = it hung
any traceback = real error, not hang
4. If step 3 hangs, test raw libiio context

timeout 15s python3 -u -c "import iio; print('before context', flush=True); ctx=iio.Context('ip:192.168.2.1'); print('after context', len(ctx.devices), flush=True)"
echo $?
This tells us whether the problem is:

libiio/Python context open itself
or pyadi’s ad9361 wrapper on top
5. If step 3 passes, run one finite Pi-local TDD capture

timeout 20s env MPLBACKEND=Agg python3 -u ~/sentinel_modeA_ref_tdd.py \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --no-plot \
  --frames 1 \
  --save-data \
  --save-path /tmp/ref_capture.npy
echo $?
With the patched script, you should now see stage lines like:

[ref] Opening Pluto ...
[ref] Opening CN0566 ...
[ref] Pluto URI: ip:192.168.2.1
[ref] Capturing first burst ...
[ref] First burst captured
6. If /tmp/ref_capture.npy exists, copy it back

ls -lh /tmp/ref_capture*
exit
Back on your PC:

cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp analog@phaser.local:/tmp/ref_capture.npy .
scp analog@phaser.local:/tmp/ref_capture_config.npy .
python3 offline_replay.py ref_capture.npy
What to send me

output of step 3
output of step 4 if step 3 hangs
full stage output of step 5
result of ls -lh /tmp/ref_capture*
That will tell us exactly whether the blocker is:

pyadi Pluto open on the Pi
TDD object bring-up
first burst capture
or only later processing




7:45 PM
2 files changed
+66
-34
Undo

Review
workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/sentinel_modeA_ref_tdd.py
workspace/session_log.md
for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ ssh analog@phaser.local
Linux phaser 5.10.63-v7l+ #1 SMP Fri Mar 10 16:35:58 UTC 2023 armv7l

The programs included with the Debian GNU/Linux system are free software;
the exact distribution terms for each program are described in the
individual files in /usr/share/doc/*/copyright.

Debian GNU/Linux comes with ABSOLUTELY NO WARRANTY, to the extent
permitted by applicable law.
Last login: Sun Nov 23 13:27:38 2025
analog@phaser:~ $ python3 -c "import adi, numpy, matplotlib; print(adi.__version__)"
0.0.20
analog@phaser:~ $ MPLBACKEND=Agg python3 ~/sentinel_modeA_ref_tdd.py \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --save-data \
  --save-path /tmp/ref_capture.npy
0.0.20
[ref] Pluto URI: ip:192.168.2.1
file not found, loading default (all gain at maximum)
file not found, loading default (no phase shift)
[ref] rf=10.2500 GHz  bw=500 MHz  ramp=300 us  chirps=256
[ref] PRI=0.500 ms  N_frame=2000  good_ramp_samples=1079  start_offset_samples=119
[ref] rx_buffer_size=524288  buffer_time=131.1 ms  R_res=0.300 m  v_res=0.114 m/s
Traceback (most recent call last):
  File "/home/analog/sentinel_modeA_ref_tdd.py", line 368, in <module>
    sys.exit(main())
  File "/home/analog/sentinel_modeA_ref_tdd.py", line 281, in main
    raw = get_radar_data(
  File "/home/analog/sentinel_modeA_ref_tdd.py", line 139, in get_radar_data
    data = my_sdr.rx()
  File "/usr/local/lib/python3.9/dist-packages/adi/rx_tx.py", line 275, in rx
    data = self.__rx_complex()
  File "/usr/local/lib/python3.9/dist-packages/adi/rx_tx.py", line 240, in __rx_complex
    x = self._rx_buffered_data()
  File "/usr/local/lib/python3.9/dist-packages/adi/compat.py", line 149, in _rx_buffered_data
    self._rxbuf.refill()
  File "/usr/local/lib/python3.9/dist-packages/iio.py", line 1003, in refill
    _buffer_refill(self._buffer)
  File "/usr/local/lib/python3.9/dist-packages/iio.py", line 62, in _check_negative
    raise OSError(-result, _strerror(-result))
TimeoutError: [Errno 110] Connection timed out
analog@phaser:~ $ ls
89-pluto.rules  hostname                plutosdr-fw-v0.38.zip   sentinel_modeA_ref_tdd.py
Bookshelf       hosts                   pluto_update_ad9361.sh  sentinel_radar.py
config.txt      iiod-usb@.service       Public                  target_detection_dbfs.py
copyright       img                     pyadi-iio               Templates
Desktop         LICENSE.html            __pycache__             Videos
Documents       Music                   radar_backends.py
Downloads       phaser_sdcard_setup.sh  radar_dsp.py
hats            Pictures                radar_mode_a.py
analog@phaser:~ $ exit
logout
Connection to phaser.local closed.
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp sentinel_modeA_ref_tdd.py analog@phaser.local:~/
sentinel_modeA_ref_tdd.py                                     100%   14KB   6.2MB/s   00:00    
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ ssh analog@phaser.local
pkill -f sentinel_modeA_ref_tdd.py || true
Linux phaser 5.10.63-v7l+ #1 SMP Fri Mar 10 16:35:58 UTC 2023 armv7l

The programs included with the Debian GNU/Linux system are free software;
the exact distribution terms for each program are described in the
individual files in /usr/share/doc/*/copyright.

Debian GNU/Linux comes with ABSOLUTELY NO WARRANTY, to the extent
permitted by applicable law.
Last login: Sun Nov 23 13:27:50 2025 from 192.168.1.100
analog@phaser:~ $ pkill -f sentinel_modeA_ref_tdd.py || true
analog@phaser:~ $ timeout 15s python3 -u -c "import adi; print('before ad9361', flush=True); s=adi.ad9361(uri='ip:192.168.2.1'); print('after ad9361', flush=True)"
echo $?
before ad9361
after ad9361
0
analog@phaser:~ $ timeout 15s python3 -u -c "import iio; print('before context', flush=True); ctx=iio.Context('ip:192.168.2.1'); print('after context', len(ctx.devices), flush=True)"
echo $?
before context
after context 6
0
analog@phaser:~ $ timeout 20s env MPLBACKEND=Agg python3 -u ~/sentinel_modeA_ref_tdd.py \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --no-plot \
  --frames 1 \
  --save-data \
  --save-path /tmp/ref_capture.npy
echo $?
0.0.20
[ref] Opening Pluto on ip:192.168.2.1 ...
[ref] Opening CN0566 on ip:phaser.local ...
124
analog@phaser:~ $ ls -lh /tmp/ref_capture*
exit
ls: cannot access '/tmp/ref_capture*': No such file or directory
logout
Connection to phaser.local closed.
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp analog@phaser.local:/tmp/ref_capture.npy .
scp analog@phaser.local:/tmp/ref_capture_config.npy .
python3 offline_replay.py ref_capture.npy
scp: /tmp/ref_capture.npy: No such file or directory
scp: /tmp/ref_capture_config.npy: No such file or directory
[offline_replay] source not found: /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso/ref_capture.npy
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ 

Show more
7:47 PM


12 previous messages
Good. This updates the diagnosis.

We now know:

direct Pluto open on the Pi works
raw libiio context on the Pi works
Pi-local direct TDD still times out at the first my_sdr.rx()
So the remaining issue is not the PC transport anymore. It is the TDD burst/trigger path itself. Serial vs Ethernet between PC and Pi will not fix this specific failure.

Use radar_mode_a.py on the Pi for the next tests, not sentinel_mode_a.py yet:

it already exposes --rpi-uri / --sdr-uri radar_mode_a.py (line 154)
--no-tdd-ext-sync flips both TDD trigger knobs false radar_mode_a.py (line 352)
First copy the latest Pi-side files:

cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp radar_mode_a.py radar_backends.py radar_dsp.py analog@phaser.local:~/
Then SSH in:

ssh analog@phaser.local
Run these three tests in order.

1. Direct Pluto, continuous, 1 frame

timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq continuous \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name cont_direct
echo $?
2. Direct Pluto, TDD, internal trigger, 1 frame

timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq tdd \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --no-tdd-ext-sync \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name tdd_internal
echo $?
3. Direct Pluto, TDD, external trigger, 1 frame

timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq tdd \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name tdd_external
echo $?
Then inspect what got saved:

find /tmp/radar_test -maxdepth 2 -type f | sort
If any session directory was created, copy it back:

exit
cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp -r analog@phaser.local:/tmp/radar_test .
Replay on the PC:

python3 offline_replay.py radar_test/cont_direct
python3 offline_replay.py radar_test/tdd_internal
python3 offline_replay.py radar_test/tdd_external
How to interpret it:

cont_direct passes, tdd_internal passes: TDD is viable locally if we use internal trigger. Then Pi-capture + PC-GUI is a real path.
cont_direct passes, both TDD runs fail: direct RX is fine, TDD path is the real blocker. Streaming architecture will not solve TDD by itself.
cont_direct fails too: the issue is lower than TDD, in direct acquisition / bring-up / RF chain.
Send me:

console output from tests 1, 2, 3
output of find /tmp/radar_test -maxdepth 2 -type f | sort
That will tell us whether to build:

Pi capture + PC display
or abandon TDD for now and focus on continuous + better sync/reconstruction




7:50 PM
for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ ssh analog@phaser.local
Linux phaser 5.10.63-v7l+ #1 SMP Fri Mar 10 16:35:58 UTC 2023 armv7l

The programs included with the Debian GNU/Linux system are free software;
the exact distribution terms for each program are described in the
individual files in /usr/share/doc/*/copyright.

Debian GNU/Linux comes with ABSOLUTELY NO WARRANTY, to the extent
permitted by applicable law.
Last login: Sun Nov 23 13:29:17 2025 from 192.168.1.100
analog@phaser:~ $ timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq continuous \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name cont_direct
echo $?
^[[B^[[A124
analog@phaser:~ $ timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq tdd \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --no-tdd-ext-sync \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name tdd_internal
echo $?
124
analog@phaser:~ $ timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq tdd \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name tdd_external
echo $?
124
analog@phaser:~ $ find /tmp/radar_test -maxdepth 2 -type f | sort
find: ‘/tmp/radar_test’: No such file or directory
analog@phaser:~ $ 

Show more
7:52 PM


2 previous messages
This is the key result:

adi.ad9361(uri='ip:192.168.2.1') works on the Pi
raw iio.Context('ip:192.168.2.1') works on the Pi
but the radar path still fails when it tries to actually capture
So the main blocker is no longer the PC transport. It is the radar acquisition path itself. That means:

trying serial instead of Ethernet to the PC will not help yet
building a Pi->PC streaming architecture right now is premature
first we need to prove that the Pi can capture radar data locally in at least one mode
One important caveat:

your radar_mode_a.py tests were not fully valid yet, because you did not recopy the updated files before running them
So do this next, in this exact order.

1. Recopy the actual files
From your PC:

cd /mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso
scp radar_mode_a.py radar_backends.py radar_dsp.py analog@phaser.local:~/
2. Prove plain Pluto RX works, not just object creation
SSH into the Pi:

ssh analog@phaser.local
Run:

timeout 15s python3 -u -c "
import adi
s=adi.ad9361(uri='ip:192.168.2.1')
s.sample_rate=int(4e6)
s.rx_lo=int(2.1e9)
s.rx_enabled_channels=[0,1]
s.gain_control_mode_chan0='manual'
s.gain_control_mode_chan1='manual'
s.rx_hardwaregain_chan0=60
s.rx_hardwaregain_chan1=60
s.rx_buffer_size=8192
print('before rx', flush=True)
x=s.rx()
print('after rx', len(x), len(x[0]), len(x[1]), flush=True)
"
echo $?
Interpretation:

if this fails, the problem is below FMCW/TDD
if this works, Pluto RX is fine and the bug is in radar setup / PLL / TDD path
3. Re-test direct continuous on the Pi with the recopied files

timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq continuous \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name cont_direct
echo $?
4. If continuous passes, then TDD is the only broken layer
Run:

timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq tdd \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --no-tdd-ext-sync \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name tdd_internal
echo $?
5. Inspect what got saved

find /tmp/radar_test -maxdepth 2 -type f | sort
Decision rule

plain rx() fails: fix low-level Pluto receive first
plain rx() passes but continuous fails: fix radar bring-up / PLL / TX / segmentation before any streaming work
continuous passes but tdd fails: build around continuous for now, or debug TDD specifically
only after one local acquisition mode works should we build Pi->PC live streaming
Send me:

output of the plain before rx / after rx test
output of the recopied continuous run
output of find /tmp/radar_test -maxdepth 2 -type f | sort
That will tell us whether the next move is:

low-level Pluto RX debugging
radar bring-up debugging
or finally the Pi->PC split architecture




7:54 PM
session_log.md
/mnt/e/work/agent/workspace/ · 
+1
-0

Open

analog@phaser:~ $ timeout 15s python3 -u -c "
import adi
s=adi.ad9361(uri='ip:192.168.2.1')
s.sample_rate=int(4e6)
s.rx_lo=int(2.1e9)
s.rx_enabled_channels=[0,1]
s.gain_control_mode_chan0='manual'
s.gain_control_mode_chan1='manual'
s.rx_hardwaregain_chan0=60
s.rx_hardwaregain_chan1=60
s.rx_buffer_size=8192
print('before rx', flush=True)
x=s.rx()
print('after rx', len(x), len(x[0]), len(x[1]), flush=True)
"
echo $?
before rx
Traceback (most recent call last):
  File "<string>", line 13, in <module>
  File "/usr/local/lib/python3.9/dist-packages/adi/rx_tx.py", line 275, in rx
    data = self.__rx_complex()
  File "/usr/local/lib/python3.9/dist-packages/adi/rx_tx.py", line 240, in __rx_complex
    x = self._rx_buffered_data()
  File "/usr/local/lib/python3.9/dist-packages/adi/compat.py", line 149, in _rx_buffered_data
    self._rxbuf.refill()
  File "/usr/local/lib/python3.9/dist-packages/iio.py", line 1003, in refill
    _buffer_refill(self._buffer)
  File "/usr/local/lib/python3.9/dist-packages/iio.py", line 62, in _check_negative
    raise OSError(-result, _strerror(-result))
TimeoutError: [Errno 110] Connection timed out
1
analog@phaser:~ $ timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq continuous \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name cont_direct
echo $?
124
analog@phaser:~ $ timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq tdd \
  --sdr-uri ip:192.168.2.1 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --no-tdd-ext-sync \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name tdd_internal
echo $?
124
analog@phaser:~ $ 

Show more
7:57 PM


5 previous messages
This is the decisive result: plain s.rx() times out on the Pi even before any radar logic.

So the current blocker is below FMCW, TDD, and GUI:

not PC transport
not plotting
not our RD math
Right now, streaming architecture is not the next step. First we need one working Pi-local Pluto receive path.

The most likely remaining branch is:

ip:192.168.2.1 can enumerate Pluto
but its streaming path is bad on this setup
so we should try the USB-backed path the repo was designed for
The repo strongly suggests that:

iiod-usb@.service (line 12) runs iiod against a usb: Pluto backend
phaser_sdcard_setup.sh (line 13) explicitly sets up that USB-sharing path on the Pi
Do these next on the Pi.

1. Scan for libiio contexts

iio_info -s
You want to see either:

a usb:... Pluto context
or a local ip:127.0.0.1:50901 path backed by iiod-usb
2. Check whether the USB-backed daemon is running

systemctl list-units --type=service | grep iiod-usb
systemctl --no-pager --full status 'iiod-usb@*'
3. If usb:... appears, test plain RX over USB backend
Replace usb:... with the actual URI from iio_info -s:

timeout 15s python3 -u -c "
import adi
uri='usb:REPLACE_ME'
print('opening', uri, flush=True)
s=adi.ad9361(uri=uri)
s.sample_rate=int(4e6)
s.rx_lo=int(2.1e9)
s.rx_enabled_channels=[0,1]
s.gain_control_mode_chan0='manual'
s.gain_control_mode_chan1='manual'
s.rx_hardwaregain_chan0=60
s.rx_hardwaregain_chan1=60
s.rx_buffer_size=8192
print('before rx', flush=True)
x=s.rx()
print('after rx', len(x), len(x[0]), len(x[1]), flush=True)
"
echo $?
4. If no direct usb: URI is usable, test the local USB daemon bridge

iio_info -u ip:127.0.0.1:50901 | head -10
Then:

timeout 15s python3 -u -c "
import adi
uri='ip:127.0.0.1:50901'
print('opening', uri, flush=True)
s=adi.ad9361(uri=uri)
s.sample_rate=int(4e6)
s.rx_lo=int(2.1e9)
s.rx_enabled_channels=[0,1]
s.gain_control_mode_chan0='manual'
s.gain_control_mode_chan1='manual'
s.rx_hardwaregain_chan0=60
s.rx_hardwaregain_chan1=60
s.rx_buffer_size=8192
print('before rx', flush=True)
x=s.rx()
print('after rx', len(x), len(x[0]), len(x[1]), flush=True)
"
echo $?
5. If both still fail, verify Pluto mode from the Pi
The repo includes pluto_update_ad9361.sh (line 1), which indicates this setup expects Pluto in ad9361 / 2r2t mode.

Check whether the Pi can SSH to Pluto:

ssh root@pluto.local
If that works, run:

fw_printenv compatible
fw_printenv mode
exit
Expected:

compatible=ad9361
mode=2r2t
If not, that is a serious candidate root cause.

Decision

usb: RX works: use Pi-local USB backend for capture; then Pi->PC streaming becomes valid
127.0.0.1:50901 RX works: use the local USB daemon bridge on the Pi
only 192.168.2.1 fails: the Pluto USB-Ethernet gadget path is the problem
all RX paths fail: fix Pluto configuration / firmware / service state before any radar work
Send me:

output of iio_info -s
output of the iiod-usb service status
result of the usb: RX test or 127.0.0.1:50901 RX test
fw_printenv compatible and fw_printenv mode if you can SSH into Pluto




7:59 PM
im only pluged in via ethernet, thats what the official guides did, but official guides when using it with matlab say to ubplug usb from pi and plug it and ethernet both in pc 
Analog.comEngineerZoneAnalogDialogueMy Analog Login Log In
Analog Devices Wiki 
Wiki
Resources and Tools
Education Content
Wiki Help
Wiki Tools
search wiki

This version (26 Apr 2024 13:45) was approved by Jon Kraft.
The Previously approved version (26 Apr 2024 13:40) is available.Diff
Table of Contents
MATLAB Phaser setup guide
Getting started and setup
Setting up MATLAB
Installing MATLAB
Installing toolboxes
LibIIO package installation
Running the labs
Verify connectivity
Running scripts
Complete RADAR Example
MATLAB Phaser setup guide
Getting started and setup
If the Phaser board is not yet assembled, please visit the Quick Start Guide and watch the Unboxing/Setup Video to see how to assemble the Phaser and related boards.

The Quick Start Guide also has instructions on installing an image of ADI Kuiper Linux onto the Raspberry Pi's SD card, which will be required.

After that is done, a few modifications need to be made to the setup from the quick start guide.

Disconnect the Pluto from the Raspberry Pi, and connect the Pluto to your computer using a microUSB to USB cable, or other compatible standard
Connect the Raspberry Pi to your computer via ethernet, using the onboard ethernet port on the Raspberry Pi
Once completed, your setup should resemble the image below:

Setting up MATLAB
Installing MATLAB
If you already have a recent version of MATLAB installed, please skip ahead to the Installing toolboxes section below.
For the hardware board support packages to work, you must use Matlab version R2022b or newer. You can go to the download page by clicking here, you may be required to sign in with your MathWorks account first.



Select the release version on the left, then click the download button.

After downloading the installer, open it to run the MATLAB installation tool. During the installation process, there will be a window to select which products and toolboxes will be installed, as shown in the image below. Ensure that all the products shown in the image have been selected.

Once this has been done, continue installing MATLAB through the installer as normal.

Installing toolboxes
Once MATLAB is installed, additional toolboxes can be downloaded through the built-in Add-On Explorer. It can be found by opening MATLAB, selecting the Home tab, and clicking the three colored cubes labeled Add-Ons located near the top.

This will open the Add-On Explorer. Here, you can search for toolboxes using the search bar in the top right corner, and install them.



If you have skipped here without installing any toolboxes while installing MATLAB, or already had MATLAB installed, get these first:
Antenna Toolbox
Communications Toolbox
DSP System Toolbox
Phased Array System Toolbox (optional, but recommended)
Signal Processing Toolbox
Install these additional toolboxes:
Analog Devices, Inc. RF and Microwave Toolbox
Analog Devices, Inc. Transceiver Toolbox
Communications Toolbox Support Package for Analog Devices ADALM-Pluto Radio
MATLAB Support for MinGW-w64 C/C++ Compiler
The Communications Toolbox Support Package requires configuration, which can be performed either by following the prompt that appears after installation or by accessing the configuration tool from the add-on manager window.





Either of these options should open the configuration tool. It contains further directions for setting up the Pluto.

CAUTION: MATLAB may prompt you to update/reinstall Pluto's firmware. Do not do that here! Follow the Phaser's Quick Start Guide instructions to ensure you have the latest firmware, with the correct configuration for the Phaser.

LibIIO package installation
The LibIIO package may be needed for MATLAB to communicate and work with the Phaser. If absence of LibIIO is causing problems with MATLAB/Phaser, it can be installed from here.

Running the labs
Verify connectivity
With Phaser connected to your local network or directly to your host machine with MATLAB installed, create an instance of the adi.Phaser class from the command prompt with the IP address of the Raspberry Pi.

bf = adi.Phaser;
bf.uri = 'ip:phaser';
bf()
This will connect and configure Phaser with a default set of parameters. If you receive a connectivity error verify the Raspberry Pi is powered up and you can at least ping the device. If you are having issues reach out to our support forums on EngineerZone.

If you get an error that refers to Could not find file ad9361-wrapper.h., manually install libad9361 by running the following commands in your MATLAB command window:
A=adi.utils.libad9361
A.download_libad9361
Next verify connectivity to Pluto with a similar method. Create and instance of the adi.AD9361.Rx class and run the operator method as so:

sdr = adi.AD9361.Rx
sdr.uri = 'ip:pluto';
data = sdr();
Like the Phaser system object this operation should not generate any errors. The data vector should contain non-zero data.


If there are errors while attempting to verify connectivity, please try the following options:

Check all the packages/toolboxes listed above are installed properly
Restart MATLAB and run the code again
Disconnect and re-connect the cable to the device in question
Ensure the Raspberry Pi's SD card has ADI Kuiper Linux installed (and that it works)
Running scripts
Once both the Phaser and Pluto are able to communicate with MATLAB, download and extract phasersteeringangle.zip

Then open the file Phaser_steeringAngle_rev1.m

This script functions to scan through a range of steering angles and output a plot of the array factor.

% Key Parameters
signal_freq = 10.145e9;  % this is the HB100 frequency
signal_freq = findTxFrequency();
plutoURI = 'ip:192.168.2.1';
phaserURI = 'ip:phaser.local';
 
% Setup the pluto
rx = setupPluto(plutoURI);
 
% Setup the phaser
bf = setupPhaser(rx,phaserURI,signal_freq);
bf.RxPowerDown(:) = 0;
bf.RxGain(:) = 127;
This segment of the code serves to initialize the Pluto and Phaser objects in MATLAB using the ADI toolboxes installed earlier, here labeled as “rx” and “bf” respectively. It also scans briefly to find the frequency of the HB100 emitter.

% Create the model of the phaser    
c = physconst('LightSpeed');
phaserModel = phased.ULA('NumElements',8,'ElementSpacing', ... 
    bf.ElementSpacing);
steeringVec = phased.SteeringVector("SensorArray",phaserModel, ...
    'NumPhaseShifterBits',7,'PropagationSpeed',c);
This segment creates a model of the antenna array on the Phaser, using the Phased Array System Toolbox (phased) from MathWorks. The Phaser features 8 uniformly spaced elements, which is modeled using the Uniform Linear Array object (phased.ULA) from the Phased Array System Toolbox. A corresponding steering vector is created using the SteeringVector object, also from the Phased Array System Toolbox.

%% Set all gains to max and phases to zero
bf.RxGain(:) = 127;   % max gain = 127, min gain = 0
bf.RxAttn(:) = 0;     % if RxAttn=1 then insert 20dB attenuator
bf.RxPhase(:) = 0;
bf.LatchRxSettings(); % write new settings to the ADAR1000s
 
% Load Phase calibration values
PhaseCal = [0; -8.4375; -5.625; -5.625; 67.5; 87.1875; 90; 101.25];
This segment just sets the gain levels and phase calibration values.

%% Sweep the steering angle and capture data
steeringAngle = -90 : 90;
ArrayFactor = zeros(size(steeringAngle));
for ii = 1 : numel(steeringAngle)
    arrayWeights = steeringVec(signal_freq,steeringAngle(ii));
    phases = rad2deg(angle(conj(arrayWeights(:))));
    phases = phases - phases(1);
    phases = phases + PhaseCal;
    phases = wrapTo360(phases);
    bf.RxPhase(:) = phases.';
    bf.LatchRxSettings();
    receivedSig_HW = rx();
    receivedSig_HW_sum = sum(receivedSig_HW,2);
    receivedFFT = fft(receivedSig_HW_sum);
    ArrayFactor(ii) = (max(abs(receivedFFT)));
end
Here is where the actual beam steering action happens in the code.
The code creates an array containing the angles that the beam will be steered through. Then, it performs a loop where:

Takes a given angle from the array of angles
Use the given steering angle to create another array containing the respective phase shifts to be applied to each antenna element
Apply the phase shifts to the Phaser
Collect data from the Pluto, perform FFT on the data to get the max amplitude, and record it
Repeat for the next steering angle
%% Compare the measured array factor and model
[~,ind] = max(ArrayFactor);
EmitterAz = steeringAngle(ind)
figure(101)
arrayWeights = steeringVec(signal_freq,EmitterAz);
pattern(phaserModel,signal_freq,-90:90,0,'CoordinateSystem', ...
    'Rectangular','Type','powerdb','weights',arrayWeights)
hold on;
 
% Plot the measured data and the model
plot(steeringAngle,mag2db(ArrayFactor./max(abs(ArrayFactor))))
Here, the Phased Array System Toolbox is used to simulate the array factor for the Phaser. Then both the experimentally obtained array factor (from the data above) and the simulated array factor are plotted. The resulting plot that appears should resemble the image below. Note that differences between the simulated and obtained array factor are mostly due to the fact that calibration has not been performed yet.

Complete RADAR Example
Once you have everything above installed, and working, then proceed to the MathWork's “Phaser Control with MATLAB” page: https://github.com/mathworks/Phaser-Control-with-MATLAB

This is a complete tutorial on how to implement more advanced radar functions, like multi chirp range doppler plotting, in MATLAB. Note: these scripts will require the “Phased Array System Toolbox” to be installed.

phaser_matlab.txt · Last modified: 26 Apr 2024 13:44 by Jon Kraft
Page Tools
﻿
Analog.comEngineerZoneAnalogDialogueMy Analog Login Log In
Analog Devices Wiki 
Wiki
Resources and Tools
Education Content
Wiki Help
Wiki Tools
search wiki

This version (29 Mar 2025 15:12) was approved by Jon Kraft.
The Previously approved version (06 Jan 2025 18:28) is available.Diff
Table of Contents
EVAL-CN0566-RPIZ Quick Start Guide
Unboxing / Setup Video
Equipment Required
Overview Photo
SD card / Software Setup
Assembling, Booting and Configuring the System
Configuring the SD Card
Software Quick Start
Initial Calibration
Appendix: Pluto Setup
Appendix: Configuration Script Notes
EVAL-CN0566-RPIZ Quick Start Guide
CN0566 “Phaser” Quick Start Guide.

This is the minimum amount of setup to get the phaser up and running.

Unboxing / Setup Video

Equipment Required
Hardware
EVAL-CN0566-RPIZ Board
Raspberry Pi 4
ADALM-Pluto
5 V, 3 A, USB-C wall adapter
HB100 microwave source
5V benchtop supply or 3 AAA (or AA) cells for HB100 power
Micro HDMI to HDMI cable (or suitable adapters)
16GB or larger SD card
Tripod
One of the following:
Ethernet cable and DHCP-enabled network with internet access
Wireless network
One of the following:
USB keyboard and mouse, HDMI display (if running locally)
Host computer with SSH client and / or VNC client.
Software
ADI Kuiper Linux image
Other
Wired or Wireless network connection, with access to the internet (for accessing update files from Github.)


This is the step file for the HB100 holder, with a 1/4-20 thread for a standard camera mount. HB100s can vary from manufacturer to manufacturer, so please do a quick measurement on the one you have, and make any necessary adjustments to the step file.
Alternatively, use one of the other mounting methods described in the quick-start video.
HB100 Holder Step File

Overview Photo

Figure 2. EVAL-CN0566-RPIZ Circuit Evaluation Block assignment


SD card / Software Setup
In order for the Raspberry Pi to control the Phaser devices, you will need to write the ADI Kuiper Linux image to an SD card and configure it. The SD card that ships with the phaser kit MUST be updated with a new image. The Phaser software is tested with Kuiper Linux 2021_R2, which, in spite of the “2021” in the name, was released in Spring, 2023. While there are newer Kuiper Linux releases, this version is required to maintain compatibility with the current phaser software packages.

Download the image from:

2 April 2023 release (2021_r2)
Actual file
Checksum image_2023-04-02-ADI-Kuiper-full.zip: 0cdcf6e131318113a137cf54335b9614
Checksum 2023-04-02-ADI-Kuiper-full.img: aeff476b577b45cc6ce6ce02403a57c2
Complete instructions for how to write it to the SD card and how to configure an example system are provided at Analog Devices Kuiper Linux.
Hints: The Kuiper Linux wiki pages give a few options, but the “official” Raspberry Pi imager is very straightforward:
Raspberry Pi Imager

Switch to ADI Kuiper Imager once it's stable and documented on the wiki.

After writing the image, if a window pops up saying “this card needs to be formatted, would you like to format it now?”, the answer is NO. Eject the card and insert it into the Raspberry Pi's SD card slot.

Assembling, Booting and Configuring the System

Figure 3. Test Setup Functional Block Diagram

Connect ADALM-Pluto's CENTER micro-USB port to Raspberry Pi via micro-USB to USB cable.
CAREFULLY thread the tripod into the tripod mount. Please minmize stress on the tripod mount while plugging in cables and other operations, as it is screwed directly to the PC board.
Verify that the SD card is properly inserted into the slot on the Raspberry Pi.
EITHER:
Connect an HDMI display, USB keyboard, and USB mouse to the Raspberry pi, OR
For remote login, connect the Raspberry Pi's Ethernet jack to your wired network, or directly to a host computer's Ethernet jack
Power up the setup through the type-C port of the CN0566. Do NOT connect a supply to the Raspberry Pi.
Wait for Raspberry Pi to boot. (This may take a minute or two, as the filesystem is expanded on first boot.)
After booting, if you are using a keyboard and local display open a terminal as seen on the taskbar. If you are logging in over the network connection, you can open a graphical desktop VNC session or command line SSH session to hostname analog.local
Configuring the SD Card
The easiest way to configure the SD card is by running a setup script. This does require a wired or wireless internet connection, but it is much easier than doing things manually. A wired connection is fairly straightforward if your network supports DHCP. If you need to use a wireless connection, you'll have to boot to the graphical desktop and connect manually.
Once connected to a network, run the following commands (and take a look at the setup script if you're suspicious, and note that there may be some updates as newer versions of Kuiper Linux are released.) The script is commented if you want a detailed description of what it's doing.

wget https://github.com/thorenscientific/rpi_setup_stuff/raw/main/phaser/phaser_sdcard_setup.sh
sudo chmod +x phaser_sdcard_setup.sh
./phaser_sdcard_setup.sh
sudo reboot
(Wait for the system to reboot)
Note: After running the script, the hostname will be phaser.local
If you are going to be running scripts and other software directly on the Raspberry Pi, it's a good idea set a few options using the Raspberry Pi configuration utility. This can be accessed from the Start Menu under Preferences, or by running:

sudo raspi-config
from the command line. Set the locale, keyboard, timezone, and wifi country (if you'll be connecting to your network by wifi.)

You'll also need to downgrade the numpy version to fix a compatibility issue with our existing example python scripts:

sudo pip install --force-reinstall numpy==1.22


Software Quick Start
At this point, the GUI can be run from the command line. Power up the HB100 source with either a 3V benchtop power supply or two AA cells and aim it a the phaser antenna. Run the following command to find the HB100 frequency:

cd ~/pyadi-iio/examples/phaser
python phaser_find_hb100.py
Ideally, there should be a single, sharp peak as shown in the figure below.



Figure 1. Find HB100 script

If there is a single prominent peak, enter “y”. If there are several peaks or no visible peak, close the plot and enter 'n' at the prompt. Reposition the HB100 (and make sure there are no other sources nearby), then re-run the script.
Next run the GUI:

cd ~/pyadi-iio/examples/phaser
python phaser_gui.py
The GUI should load and begin displaying the beam pattern as shown below.
Figure x. Phaser GUI

Initial Calibration
The phaser board is initially uncalibrated; each element will have a slightly different gain and slight phase error due to numerous factors. The phaser_examples.py script provides a calibration utility that will generate calibration files. Shut down the GUI if it is running. Place the HB100 directly in front of the array at mechanical boresight , approximately 1.5 m away. Then run:

cd ~/pyadi-iio/examples/phaser
python phaser_examples.py cal
The script provides debug information and plots as it is running, you may have to close out of each plot for the script to proceed. After running this script, files gain_cal_val.pkl and phase_cal_val.pkl will be placed in the working directory. The GUI program will also load these files automatically when run again.

Refer to CN0566 Calibration for additional details

Appendix: Pluto Setup
The Pluto that ships with the phaser kit has been pre-configured. In case something goes wrong, here is how to update the firmware and settings. For the CN0566, the TDD engine and additional control signals are required for some configurations, and were added as of Pluto firmware 0.38. The latest firmware is available from:

plutosdr-fw/releases

The next step is to update the Pluto configuration to enable the AD9361's second channel. Follow the directions at:
Updating to the AD9364,
For setting the mode of a Rev. C PlutoSDR to 2r2t, the following would be sequence of commands:

fw_setenv attr_name compatible
fw_setenv attr_val ad9361
fw_setenv compatible ad9361
fw_setenv mode 2r2t
reboot
Verify that the configuration was programmed properly by entering the following commands:

fw_printenv attr_name
fw_printenv attr_val
fw_printenv compatible
fw_printenv mode
Wihich should return:

# fw_printenv attr_name
attr_name=compatible
# fw_printenv attr_val
attr_val=ad9361
# fw_printenv compatible
compatible=ad9361
# fw_printenv mode
mode=2r2t
#
Appendix: Configuration Script Notes
To manually edit config.txt, add the following:

# Phaser board overlay:
dtoverlay=rpi-cn0566

# Heartbeat blinky:
dtparam=act_led_gpio=26
dtparam=act_led_trigger=heartbeat

# Short GPIO121 (pin 40) to ground for shutdown:
dtoverlay=gpio-shutdown,gpio_pin=21,active_low=1,gpiopull=up
If you will be logging in via VNC, see this article:
RPi 4 4gb slow VNC
Also if running “headless” without a monitor, set the HDMI group and mode accordingly:

# dtoverlay=vc4-kms-v3d

# uncomment to force a specific HDMI mode (this will force 1920x1080)
hdmi_group=2
hdmi_mode=82
End of Document

resources/eval/user-guides/circuits-from-the-lab/cn0566/quickstart.txt · Last modified: 29 Mar 2025 15:12 by Jon Kraft
Page Tools
﻿

----------


nalog@phaser:~ $ iio_info -s
Library version: 0.24 (git tag: accb7b5)
Compiled with backends: local xml ip usb serial
Available contexts:
        0: 192.168.10.2 (cpu_thermal,rpi_volt,one-bit-adc-dac,adf4159,adar1000_1,adar1000_0,ad7291) [ip:phaser.local]
        1: 192.168.137.213 (cpu_thermal,rpi_volt,one-bit-adc-dac,adf4159,adar1000_1,adar1000_0,ad7291) [ip:phaser.local]
        2: 192.168.2.10 (cpu_thermal,rpi_volt,one-bit-adc-dac,adf4159,adar1000_1,adar1000_0,ad7291) [ip:phaser.local]
        3: 192.168.10.2 (Analog Devices PlutoSDR Rev.C (Z7010-AD9361)), serial=10447318ac0f001714001300bbad17d17d [ip:phaser.local:50901]
        4: 192.168.137.213 (Analog Devices PlutoSDR Rev.C (Z7010-AD9361)), serial=10447318ac0f001714001300bbad17d17d [ip:phaser.local:50901]
        5: 192.168.2.10 (Analog Devices PlutoSDR Rev.C (Z7010-AD9361)), serial=10447318ac0f001714001300bbad17d17d [ip:phaser.local:50901]
        6: 192.168.2.1 (Analog Devices PlutoSDR Rev.C (Z7010-AD9361)), serial=10447318ac0f001714001300bbad17d17d [ip:pluto.local]
        7: (adar1000_0, adf4159, ad7291, adar1000_1, one-bit-adc-dac on Raspberry Pi 4 Model B Rev 1.5) [local:]
        8: 0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial=10447318ac0f001714001300bbad17d17d [usb:1.3.5]
analog@phaser:~ $ systemctl list-units --type=service | grep iiod-usb
systemctl --no-pager --full status 'iiod-usb@*'
  iiod-usb@001.003.5.service                                  loaded active running IIO Daemon
● iiod-usb@001.003.5.service - IIO Daemon
     Loaded: loaded (/etc/systemd/system/iiod-usb@.service; static)
     Active: active (running) since Sun 2025-11-23 13:27:29 EST; 14min ago
       Docs: man:iiod(8)
   Main PID: 571 (iiod)
      Tasks: 3 (limit: 3694)
        CPU: 131ms
     CGroup: /system.slice/system-iiod\x2dusb.slice/iiod-usb@001.003.5.service
             └─571 /usr/sbin/iiod -p 50901 -u usb:001.003.5

Nov 23 13:27:29 phaser systemd[1]: Started IIO Daemon.
Nov 23 13:27:30 phaser iiod[571]: ERROR: host phaser
analog@phaser:~ $ timeout 15s python3 -u -c "
import adi
uri='usb:REPLACE_ME'
print('opening', uri, flush=True)
s=adi.ad9361(uri=uri)
s.sample_rate=int(4e6)
s.rx_lo=int(2.1e9)
s.rx_enabled_channels=[0,1]
s.gain_control_mode_chan0='manual'
s.gain_control_mode_chan1='manual'
s.rx_hardwaregain_chan0=60
s.rx_hardwaregain_chan1=60
s.rx_buffer_size=8192
print('before rx', flush=True)
x=s.rx()
print('after rx', len(x), len(x[0]), len(x[1]), flush=True)
"
echo $?
opening usb:REPLACE_ME
ERROR: Bad URI: 'usb:REPLACE_ME'
Traceback (most recent call last):
  File "/usr/local/lib/python3.9/dist-packages/adi/context_manager.py", line 36, in __init__
    self._ctx = iio.Context(self.uri)
  File "/usr/local/lib/python3.9/dist-packages/iio.py", line 1328, in __init__
    self._context = _new_uri(_context.encode("ascii"))
  File "/usr/local/lib/python3.9/dist-packages/iio.py", line 56, in _check_null
    raise OSError(err, _strerror(err))
OSError: [Errno 22] Invalid argument

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "<string>", line 5, in <module>
  File "/usr/local/lib/python3.9/dist-packages/adi/rx_tx.py", line 734, in __init__
    rx_def.__init__(self, *args, **kwargs)
  File "/usr/local/lib/python3.9/dist-packages/adi/rx_tx.py", line 653, in __init__
    shared_def.__init__(self, *args, **kwargs)
  File "/usr/local/lib/python3.9/dist-packages/adi/rx_tx.py", line 603, in __init__
    context_manager.__init__(self, uri_ctx, self._device_name)
  File "/usr/local/lib/python3.9/dist-packages/adi/context_manager.py", line 38, in __init__
    raise Exception("No device found")
Exception: No device found
1
analog@phaser:~ $ iio_info -u ip:127.0.0.1:50901 | head -10
Library version: 0.24 (git tag: accb7b5)
Compiled with backends: local xml ip usb serial
IIO context created with network backend.
Backend version: 0.26 (git tag: v0.26)
Backend description string: 127.0.0.1 Linux (none) 6.1.0-gf3da30df6004 #5 SMP PREEMPT Tue Oct 15 10:50:00 CEST 2024 armv7l
IIO context has 9 attributes:
        hw_model: Analog Devices PlutoSDR Rev.C (Z7010-AD9361)
        hw_model_variant: 1
        hw_serial: 10447318ac0f001714001300bbad17d17d
        fw_version: v0.39
analog@phaser:~ $ timeout 15s python3 -u -c "
import adi
uri='ip:127.0.0.1:50901'
print('opening', uri, flush=True)
s=adi.ad9361(uri=uri)
s.sample_rate=int(4e6)
s.rx_lo=int(2.1e9)
s.rx_enabled_channels=[0,1]
s.gain_control_mode_chan0='manual'
s.gain_control_mode_chan1='manual'
s.rx_hardwaregain_chan0=60
s.rx_hardwaregain_chan1=60
s.rx_buffer_size=8192
print('before rx', flush=True)
x=s.rx()
print('after rx', len(x), len(x[0]), len(x[1]), flush=True)
"
echo $?
opening ip:127.0.0.1:50901
before rx
Traceback (most recent call last):
  File "<string>", line 15, in <module>
  File "/usr/local/lib/python3.9/dist-packages/adi/rx_tx.py", line 275, in rx
    data = self.__rx_complex()
  File "/usr/local/lib/python3.9/dist-packages/adi/rx_tx.py", line 240, in __rx_complex
    x = self._rx_buffered_data()
  File "/usr/local/lib/python3.9/dist-packages/adi/compat.py", line 149, in _rx_buffered_data
    self._rxbuf.refill()
  File "/usr/local/lib/python3.9/dist-packages/iio.py", line 1003, in refill
    _buffer_refill(self._buffer)
  File "/usr/local/lib/python3.9/dist-packages/iio.py", line 62, in _check_negative
    raise OSError(-result, _strerror(-result))
TimeoutError: [Errno 110] Connection timed out
1
analog@phaser:~ $ ssh root@pluto.local
The authenticity of host 'pluto.local (192.168.2.1)' can't be established.
ECDSA key fingerprint is SHA256:dTkQBu3USxz+MIgRDpdVkKcw3tE/3usbZtZTD1D55VY.
Are you sure you want to continue connecting (yes/no/[fingerprint])? yes
Warning: Permanently added 'pluto.local' (ECDSA) to the list of known hosts.
Warning: the ECDSA host key for 'pluto.local' differs from the key for the IP address '192.168.2.1'
Offending key for IP in /home/analog/.ssh/known_hosts:1
Are you sure you want to continue connecting (yes/no)? yes
root@pluto.local's password: 
Welcome to:
______ _       _        _________________
| ___ \ |     | |      /  ___|  _  \ ___ \
| |_/ / |_   _| |_ ___ \ --.| | | | |_/ /
|  __/| | | | | __/ _ \ --. \ | | |    /
| |   | | |_| | || (_) /\__/ / |/ /| |\ \
\_|   |_|\__,_|\__\___/\____/|___/ \_| \_|

v0.39
https://wiki.analog.com/university/tools/pluto
# fw_printenv compatible
compatible=ad9361
# fw_printenv mode
mode=2r2t
# exit
Connection to pluto.local closed.
analog@phaser:~ $ fw_printenv compatible
fw_printenv mode
exit
Configuration file wrong or corrupted
Configuration file wrong or corrupted
logout
Connection to phaser.local closed.
(.venv) (base) kayoum@kayoum:/mnt/e/work/agent/workspace/SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing/RADAR/phaser_perso$ 

Show more
8:03 PM


2 previous messages
That changes the picture.

What we know now:

Pluto is configured correctly: compatible=ad9361, mode=2r2t
Pluto can be discovered over:
ip:192.168.2.1
ip:127.0.0.1:50901
usb:1.3.5
But even a minimal s.rx() times out over:
ip:192.168.2.1
ip:127.0.0.1:50901
So:

serial is not the answer
PC streaming architecture is not the immediate next step
the remaining credible branches are:
Test the actual direct usb: backend on the Pi
If that fails, stop fighting the Pi-attached Pluto topology and switch to the official MATLAB-style setup:
Pluto directly to PC
Pi only for Phaser control over Ethernet
You accidentally skipped the first one by leaving usb:REPLACE_ME in the command. Use the real URI from iio_info -s.

Do this next on the Pi

Get the exact USB URI first:

iio_info -s | grep '\[usb:'
You already have:

usb:1.3.5
Now test plain receive over direct USB backend:

timeout 15s python3 -u -c "
import adi
uri='usb:1.3.5'
print('opening', uri, flush=True)
s=adi.ad9361(uri=uri)
s.sample_rate=int(1e6)
s.rx_lo=int(2.1e9)
s.rx_enabled_channels=[0]
s.gain_control_mode_chan0='manual'
s.rx_hardwaregain_chan0=60
s.rx_buffer_size=4096
print('before rx', flush=True)
x=s.rx()
print('after rx', type(x).__name__, len(x), flush=True)
"
echo $?
Then, if that works, test the radar script over the same direct USB URI:

timeout 25s env MPLBACKEND=Agg python3 -u ~/radar_mode_a.py \
  --acq continuous \
  --sdr-uri usb:1.3.5 \
  --rpi-uri ip:phaser.local \
  --ramp-time-us 300 \
  --num-chirps 64 \
  --frames 1 \
  --no-plot \
  --save-dir /tmp/radar_test \
  --session-name cont_usb
echo $?
Then:

find /tmp/radar_test -maxdepth 2 -type f | sort
Interpretation

If usb:1.3.5 plain rx() works:
the network/gadget path is the issue
Pi-local capture is still viable
then Pi capture + PC display becomes a sensible next architecture
If usb:1.3.5 plain rx() also fails:
the Pi-attached Pluto path is fundamentally broken for data capture on this system
then the best next move is to physically switch to the official MATLAB topology:
unplug Pluto from Pi
connect Pluto directly to PC via USB
keep Pi on Ethernet as phaser.local
let PC own Pluto, Pi own Phaser
And yes, your reading of the official MATLAB guide is important: it is not using the same topology you are using now. It explicitly moves Pluto off the Pi and onto the host PC. That now looks like a serious clue, not a side detail.

So the next test is just this

direct USB backend on the Pi using usb:1.3.5
Send me:

output of the usb:1.3.5 plain rx() test
output of the cont_usb run if the plain test passes
If usb:1.3.5 fails too, I’ll give you the exact physical rewiring and host-side test sequence for the official PC-direct-Pluto topology.





8:04 PM
session_log.md
/mnt/e/work/agent/workspace/ · 
+1
-0

Open

