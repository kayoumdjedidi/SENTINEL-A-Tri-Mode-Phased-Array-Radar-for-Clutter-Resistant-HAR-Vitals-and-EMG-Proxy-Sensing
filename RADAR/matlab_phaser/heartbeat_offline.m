%% OFFLINE HEARTBEAT / MICRO-DOPPLER FROM FMCW DEMO CAPTURE
% Run `fmcwDemo.m` first (it produces variables: data, prf, fs, fc, nPulses, etc.).
% This script does *no* RF configuration. It only post-processes the `data`
% matrix already in the workspace.
%
% Processing steps:
%   1) Range FFT per pulse to form beat spectrum.
%   2) Pick the brightest range bin (average across pulses).
%   3) Build a slow-time complex series from that bin.
%   4) Phase-demodulate, band-pass 0.6–2.5 Hz, convert to displacement (mm).
%   5) Plot slow-time spectrum and displacement vs time.

clearvars -except data prf fc fs nPulses nSamples tpulse sweepslope rangeResolution maxRange maxSpeed speedResolution

% ----- Sanity checks -----
if ~exist('data','var')
    error('No ''data'' in workspace. Run fmcwDemo.m first.');
end
if ~exist('prf','var')
    error('No ''prf'' in workspace. Run fmcwDemo.m first.');
end
if ~exist('fc','var')
    error('No ''fc'' (RF) in workspace. Run fmcwDemo.m first.');
end

[Nfast, Npulses] = size(data);
fprintf('Processing data of size [%d fast x %d pulses], prf=%.2f Hz\n', Nfast, Npulses, prf);

% ----- Range FFT (beat spectrum) -----
winFast = hann(Nfast,'periodic');
Nfft    = 2^nextpow2(Nfast);
beatSpec = fft(data .* winFast, Nfft, 1);   % [Nfft x Npulses]
beatSpec = beatSpec(1:Nfft/2, :);           % positive freqs

% Pick brightest range bin across pulses
magAvg = mean(abs(beatSpec), 2);
[~, kMax] = max(magAvg);
binSeries = beatSpec(kMax, :).';            % slow-time complex series (Npulses x 1)

% ----- Phase demodulation -----
% Normalize amplitude to avoid AM leakage into phase
binSeries = binSeries ./ max(abs(binSeries), 1e-9);
phi   = unwrap(angle(binSeries));
fs_sl = prf;                                % slow-time sampling rate

% Heartbeat band (adjust if measuring speaker tone)
hbBandHz = [0.6 2.5];
wp = hbBandHz/(fs_sl/2); wp(wp>=1)=0.99;
[bBP,aBP] = butter(4, wp, 'bandpass');
% Guard: need enough samples for filtfilt
if numel(phi) < 3*max(numel(bBP), numel(aBP))
    phi_bp = phi;
else
    phi_bp = filtfilt(bBP, aBP, phi);
end

% Convert phase to displacement (mm): round-trip displacement = lambda/(4*pi) * phase
c0 = physconst('LightSpeed');
lambda = c0 / fc;
disp_mm = (phi_bp * lambda) / (4*pi*1e-3);

% Slow-time spectrum for visualization
Nsl = numel(phi_bp);
Nslfft = 2^nextpow2(Nsl);
SL = fftshift(fft(phi_bp .* hann(Nsl,'periodic'), Nslfft));
fsl = (-Nslfft/2:Nslfft/2-1)'/Nslfft * fs_sl;
magSL = 20*log10(max(abs(SL), 1e-12));

% ----- Plots -----
t_sl = (0:Npulses-1)'/fs_sl;

figure('Name','Heartbeat from FMCW demo','NumberTitle','off');
subplot(3,1,1);
plot(fsl, magSL, 'LineWidth', 1.4); grid on;
xlim([0 5]); xlabel('Slow-time freq (Hz)'); ylabel('dB');
title('Slow-time spectrum (phase-demod)');

subplot(3,1,2);
plot(t_sl, disp_mm, 'm', 'LineWidth', 1.2); grid on;
xlabel('Time (s)'); ylabel('Displacement (mm)');
title('Heartbeat displacement vs time (phase-derived)');

subplot(3,1,3);
plot(t_sl, abs(binSeries), 'c', 'LineWidth', 1.0); grid on;
xlabel('Time (s)'); ylabel('|beat bin| (norm)');
title(sprintf('Selected beat bin k=%d (range cell)', kMax));

sgtitle('Post-processing of fmcwDemo.m capture (no RF changes)');
