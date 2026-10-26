package io.daxqo.app;

import android.Manifest;
import android.app.Activity;
import android.app.Instrumentation;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Bundle;
import android.os.SystemClock;

import com.k2fsa.sherpa.onnx.FeatureConfig;
import com.k2fsa.sherpa.onnx.OnlineModelConfig;
import com.k2fsa.sherpa.onnx.OnlineRecognizer;
import com.k2fsa.sherpa.onnx.OnlineRecognizerConfig;
import com.k2fsa.sherpa.onnx.OnlineRecognizerResult;
import com.k2fsa.sherpa.onnx.OnlineStream;
import com.k2fsa.sherpa.onnx.OnlineZipformer2CtcModelConfig;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;

/** Temporary device acceptance runner. No user audio is stored or transcribed. */
public final class V1Instrumentation extends Instrumentation {
    private boolean wechatPolicyOnly;
    private static final int SAMPLE_RATE = 16000;
    private static final String FIXTURE = "voice-fixture/official-zh-0.wav";
    // Upstream model release's test_wavs/0.wav; copied only into the test APK.
    // Model archive SHA256: b3b309f7ce4a737195fcc6963ea19b0653a7d3401580af5ae0d3e284cbb71f0b.

    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        wechatPolicyOnly = arguments != null && "true".equals(arguments.getString("wechatPolicyOnly"));
        start();
    }

    @Override public void onStart() {
        super.onStart();
        if (wechatPolicyOnly) { WechatGateAcceptance.run(this); return; }
        Bundle results = new Bundle();
        results.putString("fixture", "official-model-test-wav-0");
        results.putBoolean("user_audio_saved", false);
        results.putBoolean("user_audio_transcribed", false);
        boolean foreground = false, asr = false, microphone = false;

        try {
            Context target = getTargetContext();
            Intent open = new Intent(Intent.ACTION_MAIN)
                    .setComponent(new ComponentName(target.getPackageName(), MainActivity.class.getName()))
                    .addCategory(Intent.CATEGORY_LAUNCHER)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
            Activity activity = startActivitySync(open);
            waitForIdleSync();
            if (activity == null) throw new IllegalStateException("foreground_activity_unavailable");
            foreground = true;
        } catch (Throwable error) {
            results.putString("activity_error", errorType(error));
        }
        results.putBoolean("activity_pass", foreground);

        try {
            recognizeOfficialFixture(results);
            asr = true;
        } catch (Throwable error) {
            results.putString("asr_error", errorType(error));
        }
        results.putBoolean("asr_pass", asr);

        if (foreground) {
            try {
                captureMicrophoneFrames(results);
                microphone = true;
            } catch (Throwable error) {
                results.putString("microphone_error", errorType(error));
            }
        } else {
            results.putString("microphone_error", "foreground_activity_required");
        }
        results.putBoolean("microphone_pass", microphone);
        boolean success = foreground && asr && microphone;
        results.putBoolean("success", success);
        results.putString("stream", "\nV1 voice acceptance: activity=" + foreground
                + ", official_fixture_asr=" + asr + ", microphone_frames=" + microphone + "\n");
        finish(success ? Activity.RESULT_OK : Activity.RESULT_CANCELED, results);
    }

    private void recognizeOfficialFixture(Bundle results) throws Exception {
        PcmWave wave;
        try (InputStream input = getContext().getAssets().open(FIXTURE)) {
            wave = readPcmWave(input);
        }
        if (wave.sampleRate != SAMPLE_RATE || wave.samples.length == 0)
            throw new IOException("official_fixture_format_mismatch");
        results.putInt("fixture_frames", wave.samples.length);
        results.putInt("fixture_sample_rate", wave.sampleRate);

        // This is the same model/feature/provider configuration used by the
        // production VoiceInputController.createRecognizer(). Assets come from
        // the target APK, rather than a separately bundled test model.
        FeatureConfig feature = new FeatureConfig(SAMPLE_RATE, 80, 0.0f);
        OnlineModelConfig model = new OnlineModelConfig();
        model.setZipformer2Ctc(new OnlineZipformer2CtcModelConfig("voice/model.int8.onnx"));
        model.setTokens("voice/tokens.txt");
        model.setNumThreads(2);
        model.setProvider("cpu");
        model.setModelingUnit("cjkchar");
        OnlineRecognizerConfig config = new OnlineRecognizerConfig();
        config.setFeatConfig(feature);
        config.setModelConfig(model);

        OnlineRecognizer recognizer = null;
        OnlineStream stream = null;
        try {
            long started = SystemClock.elapsedRealtime();
            recognizer = new OnlineRecognizer(getTargetContext().getAssets(), config);
            stream = recognizer.createStream("");
            for (int offset = 0; offset < wave.samples.length; offset += 3200) {
                stream.acceptWaveform(Arrays.copyOfRange(wave.samples, offset,
                        Math.min(offset + 3200, wave.samples.length)), SAMPLE_RATE);
                decodeReady(recognizer, stream);
            }
            stream.inputFinished();
            decodeReady(recognizer, stream);
            OnlineRecognizerResult result = recognizer.getResult(stream);
            String transcript = result == null || result.getText() == null ? "" : result.getText().trim();
            if (transcript.isEmpty()) throw new IllegalStateException("official_fixture_transcript_empty");
            // This transcript belongs exclusively to the fixed public fixture.
            transcript = transcript.replaceAll("[\\p{Cntrl}]", " ");
            results.putString("fixture_transcript", transcript.substring(0, Math.min(160, transcript.length())));
            results.putLong("asr_elapsed_ms", SystemClock.elapsedRealtime() - started);
        } finally {
            Arrays.fill(wave.samples, 0.0f);
            if (stream != null) try { stream.release(); } catch (RuntimeException ignored) { }
            if (recognizer != null) try { recognizer.release(); } catch (RuntimeException ignored) { }
        }
    }

    private static void decodeReady(OnlineRecognizer recognizer, OnlineStream stream) {
        int steps = 0;
        while (recognizer.isReady(stream)) {
            if (++steps > 10000) throw new IllegalStateException("decoder_did_not_settle");
            recognizer.decode(stream);
        }
    }

    private void captureMicrophoneFrames(Bundle results) throws Exception {
        if (getTargetContext().checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED)
            throw new SecurityException("record_audio_permission_required");
        int minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        if (minimum <= 0) throw new IllegalStateException("pcm16_microphone_unsupported");
        AudioRecord record = null;
        short[] transientSamples = new short[SAMPLE_RATE / 5];
        int frames = 0;
        try {
            record = new AudioRecord(MediaRecorder.AudioSource.MIC, SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    Math.max(minimum * 2, 4096));
            if (record.getState() != AudioRecord.STATE_INITIALIZED)
                throw new IllegalStateException("microphone_not_initialized");
            record.startRecording();
            if (record.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING)
                throw new IllegalStateException("microphone_not_recording");
            long started = SystemClock.elapsedRealtime();
            long deadline = started + 1500;
            while (frames < transientSamples.length && SystemClock.elapsedRealtime() < deadline) {
                int count = record.read(transientSamples, 0, transientSamples.length - frames,
                        AudioRecord.READ_NON_BLOCKING);
                if (count < 0) throw new IllegalStateException("microphone_read_failed");
                frames += count;
                Arrays.fill(transientSamples, (short) 0);
                if (count == 0) SystemClock.sleep(10);
            }
            if (frames <= 0) throw new IllegalStateException("microphone_no_frames");
            results.putInt("microphone_frames", frames);
            results.putInt("microphone_sample_rate", SAMPLE_RATE);
            results.putInt("microphone_audio_ms", frames * 1000 / SAMPLE_RATE);
            results.putLong("microphone_capture_elapsed_ms", SystemClock.elapsedRealtime() - started);
        } finally {
            Arrays.fill(transientSamples, (short) 0);
            if (record != null) {
                try {
                    if (record.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) record.stop();
                } catch (RuntimeException ignored) { }
                record.release();
            }
        }
    }

    private static String errorType(Throwable error) {
        // Native/model exceptions can contain file paths. Report only a type.
        return error.getClass().getSimpleName();
    }

    private static final class PcmWave {
        final int sampleRate;
        final float[] samples;
        PcmWave(int rate, float[] values) { sampleRate = rate; samples = values; }
    }

    /** Read a bounded RIFF/WAVE fixture, including padded/unknown metadata chunks. */
    private static PcmWave readPcmWave(InputStream input) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] block = new byte[8192];
        int count;
        while ((count = input.read(block)) != -1) {
            if (output.size() + count > 4 * 1024 * 1024) throw new IOException("fixture_too_large");
            output.write(block, 0, count);
        }
        byte[] bytes = output.toByteArray();
        if (bytes.length < 12 || !fourcc(bytes, 0, "RIFF") || !fourcc(bytes, 8, "WAVE"))
            throw new IOException("fixture_not_wave");
        ByteBuffer data = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        long riffEnd = Integer.toUnsignedLong(data.getInt(4)) + 8;
        if (riffEnd > bytes.length || riffEnd < 12) throw new IOException("fixture_riff_size_invalid");
        int rate = 0, channels = 0, bits = 0, format = 0, pcmOffset = -1, pcmBytes = 0;
        for (int position = 12; position + 8L <= riffEnd; ) {
            long length = Integer.toUnsignedLong(data.getInt(position + 4));
            long end = position + 8L + length;
            if (end > riffEnd) throw new IOException("fixture_chunk_size_invalid");
            if (fourcc(bytes, position, "fmt ")) {
                if (length < 16) throw new IOException("fixture_format_invalid");
                format = Short.toUnsignedInt(data.getShort(position + 8));
                channels = Short.toUnsignedInt(data.getShort(position + 10));
                rate = data.getInt(position + 12);
                bits = Short.toUnsignedInt(data.getShort(position + 22));
            } else if (fourcc(bytes, position, "data")) {
                pcmOffset = position + 8;
                pcmBytes = (int) length;
            }
            position = (int) (end + (length & 1));
        }
        if (format != 1 || channels != 1 || bits != 16 || rate <= 0 || pcmOffset < 0 || (pcmBytes & 1) != 0)
            throw new IOException("fixture_requires_mono_pcm16");
        float[] samples = new float[pcmBytes / 2];
        for (int i = 0; i < samples.length; i++) samples[i] = data.getShort(pcmOffset + i * 2) / 32768.0f;
        Arrays.fill(bytes, (byte) 0);
        return new PcmWave(rate, samples);
    }

    private static boolean fourcc(byte[] source, int offset, String wanted) {
        if (offset < 0 || offset + 4 > source.length) return false;
        for (int i = 0; i < 4; i++) if (source[offset + i] != (byte) wanted.charAt(i)) return false;
        return true;
    }
}
