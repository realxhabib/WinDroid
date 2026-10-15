import android.graphics.Rect;
import android.view.SurfaceControl;

import java.lang.reflect.Method;

/**
 * Runs inside Android via app_process, like AppCatalog:
 *
 *   CLASSPATH=/data/local/tmp/appcatalog.jar app_process / SecureReset
 *
 * Works around a SurfaceFlinger bug in Android 16 (FrontEnd, getLayerSnapshotsForScreenshots):
 * after a layer screenshot whose root layer is secure, SurfaceFlinger recomputes every layer with
 * that "secure" root and keeps the result. Every layer on every display then counts as secure, so
 * on scrcpy's displays (which aren't secure) every app goes black and all input to it is dropped,
 * until the next layer screenshot of something that isn't secure. Android takes such screenshots of
 * its own (task snapshots), e.g. when a task with a FLAG_SECURE window moves to another display.
 *
 * This takes that next screenshot: a 1x1 capture of a layer of our own, which isn't secure and is on
 * no display, so nothing shows. Prints "ok" if SurfaceFlinger took it.
 */
public final class SecureReset {
    public static void main(String[] args) throws Exception {
        SurfaceControl layer = new SurfaceControl.Builder().setName("androiddesktop-secure-reset")
                .setBufferSize(1, 1).build();
        try {
            // android.window.ScreenCapture is hidden API; app_process may use it, like scrcpy does.
            Class<?> argsClass = Class.forName("android.window.ScreenCapture$LayerCaptureArgs");
            Class<?> builderClass = Class.forName("android.window.ScreenCapture$LayerCaptureArgs$Builder");
            Object builder = builderClass.getConstructor(SurfaceControl.class).newInstance(layer);
            builderClass.getMethod("setSourceCrop", Rect.class).invoke(builder, new Rect(0, 0, 1, 1));
            Object captureArgs = builderClass.getMethod("build").invoke(builder);
            Method capture = Class.forName("android.window.ScreenCapture").getMethod("captureLayers", argsClass);
            System.out.println(capture.invoke(null, captureArgs) != null ? "ok" : "no screenshot");
        } finally {
            layer.release();
        }
    }
}
