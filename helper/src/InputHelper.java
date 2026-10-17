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
import java.util.HashMap;

/**
 * Keys, text and the mouse wheel for app windows on the emulator's own displays, run via app_process
 * and kept running for as long as its adb shell is open:
 *
 *   CLASSPATH=/data/local/tmp/androiddesktop-input.jar app_process / InputHelper
 *
 * Everything is injected here with the window's display id, which delivers it to that display, as
 * `input -d` does, without a process per event. Keys can't go to the emulator: it has one keyboard,
 * which Android would send to whichever display has focus. Touches could (gRPC), but the emulator's
 * touchscreens say they're styluses too, and Android then offers handwriting ("Try out your stylus")
 * in place of the keyboard; these are a finger on a touchscreen. One command per line, no answers:
 *
 *   touch <display> <action 0=down 1=up 2=move> <x> <y>   one finger
 *   key <display> <action 0=down 1=up> <keycode> <repeat> <metaState>
 *   text <display> <hex UTF-8>                     typed via the virtual keyboard's key map
 *   scroll <display> <x> <y> <vscroll> <hscroll>   a mouse wheel turn at x, y (1.0 = one notch)
 *   hover <display> <x> <y>                        the mouse pointer moved there (no button down)
 *   leave <display> <x> <y>                        ...and left the window
 *   ime <display>                                  the on-screen keyboard, if any, shows on that display
 *
 * Prints "ready <version>" once it can inject.
 */
public final class InputHelper {
    static final int VERSION = 4;
    static final int INJECT_ASYNC = 0;

    static Object inputManager;
    static Method inject;
    static Method setDisplayId;
    static final KeyCharacterMap keys = KeyCharacterMap.load(KeyCharacterMap.VIRTUAL_KEYBOARD);
    static final HashMap<Integer, Long> fingerDown = new HashMap<>();  // display -> when its finger went down

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
            case "touch":
                finger(Integer.parseInt(a[1]), Integer.parseInt(a[2]), Float.parseFloat(a[3]), Float.parseFloat(a[4]));
                break;
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
                // The pointer moves there first, as a real mouse's does: Android drops a wheel turn
                // from a pointer it hasn't seen over that display yet.
                int display = Integer.parseInt(a[1]);
                float x = Float.parseFloat(a[2]), y = Float.parseFloat(a[3]);
                mouse(display, MotionEvent.ACTION_HOVER_MOVE, x, y, 0, 0);
                mouse(display, MotionEvent.ACTION_SCROLL, x, y, Float.parseFloat(a[4]), Float.parseFloat(a[5]));
                break;
            }
            case "hover":
            case "leave":
                mouse(Integer.parseInt(a[1]), a[0].equals("hover") ? MotionEvent.ACTION_HOVER_MOVE
                        : MotionEvent.ACTION_HOVER_EXIT, Float.parseFloat(a[2]), Float.parseFloat(a[3]), 0, 0);
                break;
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

    static void finger(int display, int action, float x, float y) throws Exception {
        long now = SystemClock.uptimeMillis();
        Long down = fingerDown.get(display);
        if (action == MotionEvent.ACTION_DOWN) {
            if (down != null) {  // a lift we never got: end that touch first
                finger(display, MotionEvent.ACTION_UP, x, y);
            }
            fingerDown.put(display, down = now);
        } else if (down == null) {
            return;  // a move or lift without a touch
        } else if (action == MotionEvent.ACTION_UP) {
            fingerDown.remove(display);
        }
        MotionEvent.PointerProperties[] props = {new MotionEvent.PointerProperties()};
        props[0].id = 0;
        props[0].toolType = MotionEvent.TOOL_TYPE_FINGER;
        MotionEvent.PointerCoords[] coords = {new MotionEvent.PointerCoords()};
        coords[0].x = x;
        coords[0].y = y;
        coords[0].pressure = action == MotionEvent.ACTION_UP ? 0f : 1f;
        coords[0].size = 1f;
        MotionEvent e = MotionEvent.obtain(down, now, action, 1, props, coords, 0, 0, 1f, 1f, 0, 0,
                InputDevice.SOURCE_TOUCHSCREEN, 0);
        send(e, display);
        e.recycle();
    }

    static void mouse(int display, int action, float x, float y, float vscroll, float hscroll) throws Exception {
        long now = SystemClock.uptimeMillis();
        MotionEvent.PointerProperties[] props = {new MotionEvent.PointerProperties()};
        props[0].id = 0;
        props[0].toolType = MotionEvent.TOOL_TYPE_MOUSE;
        MotionEvent.PointerCoords[] coords = {new MotionEvent.PointerCoords()};
        coords[0].x = x;
        coords[0].y = y;
        coords[0].setAxisValue(MotionEvent.AXIS_VSCROLL, vscroll);
        coords[0].setAxisValue(MotionEvent.AXIS_HSCROLL, hscroll);
        MotionEvent e = MotionEvent.obtain(now, now, action, 1, props, coords, 0, 0, 1f, 1f, 0, 0,
                InputDevice.SOURCE_MOUSE, 0);
        send(e, display);
        e.recycle();
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
