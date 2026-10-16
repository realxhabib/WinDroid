import android.os.IBinder;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;

/**
 * Keys, text and the mouse wheel for app windows on the emulator's own displays, run via app_process
 * and kept running for as long as its adb shell is open:
 *
 *   CLASSPATH=/data/local/tmp/androiddesktop-input.jar app_process / InputHelper
 *
 * The window's touches go to the emulator's touchscreen for that display (gRPC); keys can't, as the
 * emulator has one keyboard and Android would send it to whichever display has focus. So keys are
 * injected here with the window's display id, which delivers them to that display's focused window,
 * as `input -d` does, without a process per key. One command per line, no answers:
 *
 *   key <display> <action 0=down 1=up> <keycode> <repeat> <metaState>
 *   text <display> <hex UTF-8>                     typed via the virtual keyboard's key map
 *   scroll <display> <x> <y> <vscroll> <hscroll>   a mouse wheel turn at x, y (1.0 = one notch)
 *   ime <display>                                  the on-screen keyboard, if any, shows on that display
 *
 * Prints "ready <version>" once it can inject.
 */
public final class InputHelper {
    static final int VERSION = 1;
    static final int INJECT_ASYNC = 0;

    static Object inputManager;
    static Method inject;
    static Method setDisplayId;
    static final KeyCharacterMap keys = KeyCharacterMap.load(KeyCharacterMap.VIRTUAL_KEYBOARD);

    public static void main(String[] args) throws Exception {
        Class<?> imClass;
        try {
            imClass = Class.forName("android.hardware.input.InputManagerGlobal");  // Android 14+
        } catch (ClassNotFoundException e) {
            imClass = Class.forName("android.hardware.input.InputManager");
        }
        inputManager = imClass.getMethod("getInstance").invoke(null);
        inject = imClass.getMethod("injectInputEvent", InputEvent.class, int.class);
        setDisplayId = InputEvent.class.getMethod("setDisplayId", int.class);
        System.out.println("ready " + VERSION);
        System.out.flush();
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        String line;
        while ((line = in.readLine()) != null) {
            try {
                handle(line.trim().split(" "));
            } catch (Throwable t) {
                System.err.println("InputHelper: " + line + ": " + t);
            }
        }
    }

    static void handle(String[] a) throws Exception {
        switch (a[0]) {
            case "key": {
                long now = SystemClock.uptimeMillis();
                KeyEvent e = new KeyEvent(now, now, Integer.parseInt(a[2]), Integer.parseInt(a[3]), Integer.parseInt(a[4]),
                        Integer.parseInt(a[5]), KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD);
                send(e, Integer.parseInt(a[1]));
                break;
            }
            case "text": {
                int display = Integer.parseInt(a[1]);
                String text = a.length > 2 ? new String(hex(a[2]), StandardCharsets.UTF_8) : "";
                KeyEvent[] events = keys.getEvents(text.toCharArray());
                if (events == null) {  // a character the key map can't type: type what it can, one by one
                    for (char c : text.toCharArray()) {
                        KeyEvent[] one = keys.getEvents(new char[] {c});
                        if (one != null) {
                            for (KeyEvent e : one) {
                                send(e, display);
                            }
                        }
                    }
                } else {
                    for (KeyEvent e : events) {
                        send(e, display);
                    }
                }
                break;
            }
            case "scroll": {
                long now = SystemClock.uptimeMillis();
                MotionEvent.PointerProperties[] props = {new MotionEvent.PointerProperties()};
                props[0].id = 0;
                props[0].toolType = MotionEvent.TOOL_TYPE_MOUSE;
                MotionEvent.PointerCoords[] coords = {new MotionEvent.PointerCoords()};
                coords[0].x = Float.parseFloat(a[2]);
                coords[0].y = Float.parseFloat(a[3]);
                coords[0].setAxisValue(MotionEvent.AXIS_VSCROLL, Float.parseFloat(a[4]));
                coords[0].setAxisValue(MotionEvent.AXIS_HSCROLL, Float.parseFloat(a[5]));
                MotionEvent e = MotionEvent.obtain(now, now, MotionEvent.ACTION_SCROLL, 1, props, coords, 0, 0, 1f, 1f,
                        0, 0, InputDevice.SOURCE_MOUSE, 0);
                send(e, Integer.parseInt(a[1]));
                e.recycle();
                break;
            }
            case "ime": {
                // IWindowManager.setDisplayImePolicy(display, DISPLAY_IME_POLICY_LOCAL), as scrcpy does.
                IBinder binder = (IBinder) Class.forName("android.os.ServiceManager").getMethod("getService", String.class)
                        .invoke(null, "window");
                Object wm = Class.forName("android.view.IWindowManager$Stub").getMethod("asInterface", IBinder.class)
                        .invoke(null, binder);
                wm.getClass().getMethod("setDisplayImePolicy", int.class, int.class).invoke(wm, Integer.parseInt(a[1]), 0);
                break;
            }
            default:
                System.err.println("InputHelper: unknown command " + a[0]);
        }
    }

    static void send(InputEvent e, int display) throws Exception {
        setDisplayId.invoke(e, display);
        inject.invoke(inputManager, e, INJECT_ASYNC);
    }

    static byte[] hex(String s) {
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        }
        return out;
    }
}
