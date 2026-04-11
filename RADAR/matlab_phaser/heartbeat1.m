%% PHASER CW HEARTBEAT SCOPE (CN0566 + Pluto)
% Radiate a clean CW (10–10.5 GHz) on TX OUT 2 and show:
%   1) FFT around the baseband tone
%   2) Waterfall of that spectrum
%   3) Heartbeat envelope: AC-coupled |IQ| with 8 s ring buffer
% Minimal, no GUIs. Uses helpers under drafts/matlab/Phaser-Control-with-MATLAB.
%% Clear workspace and load calibration weights

clear; close all;

%% User knobs
rfGHz          = 10.10;     % Radiated RF (GHz); LO = RF + 2.2 GHz
ifFreq         = 2.2e9;     % Pluto/AD9361 IF (Hz)
sampleRate     = 0.6e6;     % Pluto ADC/DAC rate (Hz)
toneHz         = 100e3;     % Baseband CW tone (Hz)
fftSize        = 8192;
numSlices      = 60;        % Waterfall depth
fftSpanHz      = 100e3;     % +/- span for FFT plot
wfSpanHz       = 10e3;      % +/- span for waterfall
rxGain0        = 65;        % dB, manual (raise RX gain)
txAtten1       = 0;         % dB, max TX drive (watch PA headroom)
timeWindowSec  = 8;         % Heartbeat history window (s)
hbTargetFs     = 100;       % Hz, target heartbeat phase sample rate
hbBandHz       = [0.6 2.5]; % Hz, heartbeat bandpass (after phase demod)

%% Paths and helper setup
thisFile   = mfilename('fullpath');
repoRoot   = fileparts(thisFile);
addpath(genpath(fullfile(repoRoot,'Phaser-Control-with-MATLAB','shared')));
addpath(genpath(fullfile(repoRoot,'Phaser-Control-with-MATLAB','workshop','helpers')));

%% Hardware configuration using official helpers
rfHz = rfGHz * 1e9;

% Use MathWorks helpers for Pluto + Phaser bring-up
[rx,tx] = setupPluto();              % from shared/phasercontrol
bf      = setupPhaser(rx, rfHz);     % configures Phaser RX path and PLL skeleton

% Pluto RX: align with desired sample rate / frame size
rx.SamplingRate             = sampleRate;
rx.SamplesPerFrame          = fftSize;
rx.kernelBuffersCount       = 2;
rx.GainControlModeChannel0  = 'manual';
rx.GainChannel0             = rxGain0;
rx.EnableQuadratureTracking = true;
rx.EnableRFDCTracking       = true;

% Pluto TX: follow demo pattern (both channels enabled, TX2 driven)
tx.SamplingRate        = sampleRate;
tx.CenterFrequency     = rx.CenterFrequency;  % keep IF shared
tx.EnabledChannels     = [1 2];
tx.AttenuationChannel0 = -80;                % mute TX1
tx.AttenuationChannel1 = txAtten1;           % drive TX2
tx.EnableCyclicBuffers = true;
tx.DataSource          = 'DMA';

% Phaser LO: fixed CW at rfGHz, ramp disabled, TX out via Out2
bf.Frequency   = (rfHz + rx.CenterFrequency) / 4;
bf.RampMode    = "disabled";
bf.EnablePLL   = true;
bf.EnableTxPLL = true;
bf.EnableOut1  = false;   % use TX OUT 2, as in demos
bf.RxPowerDown(:) = 0;
bf.LatchRxSettings();

%% Build and load cyclic CW tone
fs = rx.SamplingRate;
N  = rx.SamplesPerFrame;
fc = round(toneHz/(fs/N)) * (fs/N);   % bin-center to avoid scalloping
t  = (0:N-1)'/fs;
iq = exp(1j*2*pi*fc*t);
iq = iq ./ max(abs(iq));             % normalize
iqMat = [complex(zeros(size(iq))) iq]; % col1 muted, col2 driven
tx(iqMat);                           % send to TX2 only
pause(0.1);                          % allow DMA/LO to settle

%% Precompute axes and buffers
freqAxis   = (-N/2:N/2-1)'/N * fs;
fftWindow  = blackman(N,'periodic');
winScale   = sum(fftWindow);
wfStore    = -120*ones(numSlices,N); % dBFS waterfall buffer

% Heartbeat decimation and buffers (phase-demod chain)
hbDecim    = max(1, round(fs / hbTargetFs)); % ~100 Hz phase stream
hbFs       = fs / hbDecim;
histLen    = max(1, round(timeWindowSec * hbFs));
bbHist     = zeros(histLen,1);              % complex history for phase-based heartbeat
timeAxis   = (0:histLen-1)'/hbFs;
spAvg      = zeros(size(freqAxis));         % running PSD smoother

% Wavelength at RF for displacement estimate
c_light    = 299792458;
lambda_rf  = c_light / rfHz;                % meters

% Heartbeat band-pass filter (0.6–2.5 Hz by default)
bpNorm     = hbBandHz / (hbFs/2);
bpNorm(bpNorm>=1) = 0.99;                   % guard Nyquist
[bHB,aHB]  = butter(4, bpNorm, 'bandpass');

figure(1); clf;
subplot(3,1,1); title('FFT around tone'); xlabel('Hz'); ylabel('dBFS');
subplot(3,1,2); title('Waterfall'); ylabel('Hz'); xlabel('Time (s)');
subplot(3,1,3); title('Heartbeat displacement (phase-derived)'); xlabel('Time (s)'); ylabel('Displacement (mm)');

%% Acquire / display loop
frameDur = N/fs;
for k = 1:400  % ~20 s at 0.05 s update
    % Robust receive: handle transient libiio/USB errors without exiting
    try
        x = rx();
    catch ME
        warning("RX poll failed (%s). Retrying...", ME.message);
        pause(0.2);
        continue;
    end
    if size(x,2) > 1
        x = x(:,1); % use RX0 only (ADI channel 1)
    end
    if isempty(x) || all(x == 0)
        warning('RX samples are all zeros/empty. Check: (1) plutoURI reachable, (2) LO locked, (3) RF path connected, (4) TX running.');
        pause(0.2);
        continue;
    end

    % FFT of raw IF/baseband (magnitude path)
    sp = fftshift(fft(x .* fftWindow)) / winScale;
    % Exponential magnitude averaging to improve SNR readability
    alpha = 0.2;
    spMag = abs(sp);
    spAvg = (1-alpha)*spAvg + alpha*spMag;
    mag = 20*log10(max(spAvg, 1e-12)/(2^11));

    subplot(3,1,1);
    plot(freqAxis, mag, 'y','LineWidth',1.2);
    xlim([fc-fftSpanHz, fc+fftSpanHz]);
    ylim([-110 0]);
    grid on;

    % Waterfall
    wfStore = [mag.'; wfStore(1:end-1,:)]; %#ok<AGROW>
    subplot(3,1,2);
    imagesc((0:numSlices-1)*frameDur, freqAxis, wfStore.');
    axis xy;
    caxis([-90 -30]);
    ylim([fc-wfSpanHz, fc+wfSpanHz]);
    colormap(turbo);

    % Heartbeat phase chain: mix -> decimate -> phase-demod
    n = (0:N-1).';
    bb = x .* exp(-1j*2*pi*fc*n/fs);
    bb = bb(1:hbDecim:end);
    shift = numel(bb);
    if shift >= histLen
        bbHist = bb(end-histLen+1:end);
    else
        bbHist = circshift(bbHist,-shift);
        bbHist(end-shift+1:end) = bb;
    end

    % Phase-based heartbeat demodulation:
    %  1) normalize amplitude
    %  2) unwrap phase
    %  3) band-pass 0.6–2.5 Hz
    %  4) convert phase -> displacement (mm)
    bbNorm = bbHist ./ max(abs(bbHist), 1e-9);
    phi    = unwrap(angle(bbNorm));
    if numel(phi) > 3*max(numel(aHB),numel(bHB))
        phi_bp = filtfilt(bHB, aHB, phi);
    else
        phi_bp = phi;
    end
    disp_mm = (phi_bp * lambda_rf) / (4*pi*1e-3); % round-trip

    subplot(3,1,3);
    plot(timeAxis, disp_mm, 'm','LineWidth',1.2);
    maxAbs = max(abs(disp_mm));
    yspan  = max(maxAbs, 1e-6);
    xend   = max(timeAxis(end), 1/hbFs);
    axis([0 xend -1.5*yspan 1.5*yspan]);
    grid on;
    title('Heartbeat displacement (phase-derived)');
    xlabel('Time (s)'); ylabel('Displacement (mm)');

    drawnow limitrate;
end

%% Cleanup (keeps CW off when closing)
release(tx);
release(rx);
