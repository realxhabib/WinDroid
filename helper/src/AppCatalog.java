import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.drawable.AdaptiveIconDrawable;
import android.graphics.drawable.Drawable;
import android.os.Looper;

import java.io.File;
import java.io.FileOutputStream;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Runs inside Android via app_process (like scrcpy's server) and asks PackageManager about the
 * launchable apps, so Windows shows the real labels and icons exactly as Android draws them.
 *
 *   CLASSPATH=/data/local/tmp/appcatalog.jar app_process / AppCatalog list
 *   CLASSPATH=/data/local/tmp/appcatalog.jar app_process / AppCatalog icons <dir> <size> <pkg>...
 *
 * "list" prints one tab-separated line per app: package, activity, system flag, last update, label.
 */
public final class AppCatalog {

    public static void main(String[] args) throws Exception {
        PackageManager pm = systemContext().getPackageManager();
        if (args.length >= 1 && args[0].equals("list")) {
            list(pm);
        } else if (args.length >= 3 && args[0].equals("icons")) {
            File dir = new File(args[1]);
            dir.mkdirs();
            int size = Integer.parseInt(args[2]);
            for (int i = 3; i < args.length; i++) {
                try {
                    writeIcon(pm, args[i], new File(dir, args[i] + ".png"), size);
                    System.out.println("ok\t" + args[i]);
                } catch (Exception e) {
                    System.out.println("fail\t" + args[i] + "\t" + e);
                }
            }
        } else {
            System.err.println("usage: AppCatalog list | icons <dir> <size> <pkg>...");
            System.exit(2);
        }
    }

    private static void list(PackageManager pm) {
        Intent intent = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> found = pm.queryIntentActivities(intent, 0);
        Set<String> seen = new HashSet<>();
        StringBuilder out = new StringBuilder();
        for (ResolveInfo ri : found) {
            String pkg = ri.activityInfo.packageName;
            if (!seen.add(pkg)) {
                continue;
            }
            ApplicationInfo app = ri.activityInfo.applicationInfo;
            boolean system = (app.flags & ApplicationInfo.FLAG_SYSTEM) != 0
                    && (app.flags & ApplicationInfo.FLAG_UPDATED_SYSTEM_APP) == 0;
            long updated = 0;
            try {
                PackageInfo info = pm.getPackageInfo(pkg, 0);
                updated = info.lastUpdateTime;
            } catch (PackageManager.NameNotFoundException ignored) {
                // uninstalled while we were listing
            }
            String label = String.valueOf(ri.loadLabel(pm)).replace('\t', ' ').replace('\n', ' ');
            out.append(pkg).append('\t').append(ri.activityInfo.name).append('\t')
                    .append(system ? 1 : 0).append('\t').append(updated).append('\t').append(label).append('\n');
        }
        System.out.print(out);
    }

    /** Adaptive icons get a rounded-square mask that suits Windows; legacy icons keep their own shape. */
    private static void writeIcon(PackageManager pm, String pkg, File file, int size) throws Exception {
        Intent intent = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER).setPackage(pkg);
        List<ResolveInfo> found = pm.queryIntentActivities(intent, 0);
        Drawable icon = found.isEmpty() ? pm.getApplicationIcon(pkg) : found.get(0).loadIcon(pm);

        Bitmap bitmap = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bitmap);
        if (icon instanceof AdaptiveIconDrawable) {
            AdaptiveIconDrawable adaptive = (AdaptiveIconDrawable) icon;
            Path mask = new Path();
            float r = size * 0.24f;
            mask.addRoundRect(new RectF(0, 0, size, size), r, r, Path.Direction.CW);
            canvas.clipPath(mask);
            // Layers are 108dp with the middle 72dp visible: draw them 1.5x larger, centred.
            int inset = Math.round(size * 0.25f);
            drawLayer(canvas, adaptive.getBackground(), -inset, size + inset);
            drawLayer(canvas, adaptive.getForeground(), -inset, size + inset);
        } else {
            icon.setBounds(0, 0, size, size);
            icon.draw(canvas);
        }
        try (FileOutputStream out = new FileOutputStream(file)) {
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, out);
        }
    }

    private static void drawLayer(Canvas canvas, Drawable layer, int from, int to) {
        if (layer != null) {
            layer.setBounds(from, from, to, to);
            layer.draw(canvas);
        }
    }

    /**
     * app_process has no Application. ActivityThread.systemMain() attaches a full one, which is what
     * loading other apps' resources needs (a bare ActivityThread leaves Resources half-built).
     */
    private static Context systemContext() throws Exception {
        if (Looper.getMainLooper() == null) {
            Looper.prepareMainLooper();
        }
        Class<?> cls = Class.forName("android.app.ActivityThread");
        Object thread = cls.getMethod("systemMain").invoke(null);
        return (Context) cls.getMethod("getSystemContext").invoke(thread);
    }
}
