// Android Desktop Camera: a Windows 11 virtual camera that lets Android see the webcam's whole picture.
//
// Android's camera driver in Google's emulator image only knows phone cameras: a 1280x960 sensor mounted
// at 90 degrees. So the emulator always asks the webcam for 1280x960, turns each frame as a phone held
// upright would (external/qemu camera-capture-windows.cpp readFrame + convert_frame_fast), which keeps only
// the 720x960 strip on the left of the frame, and Android turns that strip back upright. A 16:9 webcam
// thus shows Android a tall slice from its left side.
//
// This camera offers exactly 1280x960 NV12 and draws the PC webcam's whole picture, letterboxed, into
// that strip (REGION_*), so Android shows all of it, upright. The rest of the frame is never seen.
// For a 16:9 stream (video recording, most video calls) Android's camera driver crops the strip again,
// to its left three quarters (FIT_*), so the picture goes there: whole in photos (4:3, with a bar on
// one side) and videos alike.
//
// It's a Media Foundation media source (MFCreateVirtualCamera, Windows 11) that runs inside Windows'
// Frame Server service. While something streams from it, it reads the PC's first physical webcam.
// regsvr32 (as administrator) registers it and creates the camera for every user; regsvr32 /u removes it.
//
// Built by tools/build_camera.py.

#define NOMINMAX
#include <windows.h>
#include <unknwn.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mfvirtualcamera.h>
#include <ks.h>
#include <ksproxy.h>
#include <wrl/client.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <deque>
#include <mutex>
#include <string>
#include <vector>

#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mf.lib")
#pragma comment(lib, "mfreadwrite.lib")
#pragma comment(lib, "mfuuid.lib")
#pragma comment(lib, "mfsensorgroup.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "advapi32.lib")

#pragma comment(linker, "/EXPORT:DllGetClassObject,PRIVATE")
#pragma comment(linker, "/EXPORT:DllCanUnloadNow,PRIVATE")
#pragma comment(linker, "/EXPORT:DllRegisterServer,PRIVATE")
#pragma comment(linker, "/EXPORT:DllUnregisterServer,PRIVATE")

using Microsoft::WRL::ComPtr;

// {509ADBEB-2D1F-421D-A0CD-5159F879304A}
static const GUID CLSID_AndroidDesktopCamera = {
    0x509adbeb, 0x2d1f, 0x421d, {0xa0, 0xcd, 0x51, 0x59, 0xf8, 0x79, 0x30, 0x4a}};
static const wchar_t CLSID_STRING[] = L"{509ADBEB-2D1F-421D-A0CD-5159F879304A}";
static const wchar_t FRIENDLY_NAME[] = L"Android Desktop Camera";  // androiddesktop/camera.py NAME

// PINNAME_VIDEO_CAPTURE (ksmedia.h), here so no GUID has to be instantiated from the headers.
static const GUID PIN_VIDEO_CAPTURE = {0xfb6c4281, 0x0353, 0x11d1, {0x90, 0x5f, 0x00, 0x00, 0xc0, 0xcc, 0x16, 0xba}};

// What Android asks for, and the part of it that Android shows: a 3:4 strip on the left, full height.
static const UINT32 OUT_W = 1280, OUT_H = 960, FPS = 30;
static const UINT32 REGION_W = OUT_H * OUT_H / OUT_W, REGION_H = OUT_H;  // 720 x 960
static const UINT32 FIT_W = REGION_W * 3 / 4, FIT_H = REGION_H;           // 540 x 960, on the left
static const UINT32 FRAME_BYTES = OUT_W * OUT_H * 3 / 2;                 // NV12
static const LONGLONG FRAME_TIME = 10000000 / FPS;                       // 100 ns units
static const DWORD VIDEO = (DWORD)MF_SOURCE_READER_FIRST_VIDEO_STREAM;

static HMODULE g_module;
static std::atomic<long> g_objects{0}, g_locks{0};

// ------------------------------------------------------------------ log
// %ProgramData%\AndroidDesktop\camera-<user>.log: Frame Server runs us as LOCAL SERVICE, tests as the user.
static void Log(const wchar_t* fmt, ...) {
    static std::mutex lock;
    std::lock_guard<std::mutex> guard(lock);
    wchar_t dir[MAX_PATH], user[128] = L"user", path[MAX_PATH + 160];
    DWORD n = ARRAYSIZE(user);
    if (!ExpandEnvironmentStringsW(L"%ProgramData%\\AndroidDesktop", dir, MAX_PATH)) return;
    GetUserNameW(user, &n);
    for (wchar_t* c = user; *c; ++c)
        if (*c == L' ') *c = L'-';
    CreateDirectoryW(dir, nullptr);
    swprintf_s(path, L"%s\\camera-%s.log", dir, user);
    WIN32_FILE_ATTRIBUTE_DATA info;
    bool big = GetFileAttributesExW(path, GetFileExInfoStandard, &info) && info.nFileSizeLow > (1 << 20);
    FILE* f = nullptr;
    if (_wfopen_s(&f, path, big ? L"w" : L"a") != 0 || !f) return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    fwprintf(f, L"%04d-%02d-%02d %02d:%02d:%02d.%03d [%lu] ", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute,
             t.wSecond, t.wMilliseconds, GetCurrentProcessId());
    va_list args;
    va_start(args, fmt);
    vfwprintf(f, fmt, args);
    va_end(args);
    fputwc(L'\n', f);
    fclose(f);
}

#define CHECK(expr)                                  \
    do {                                             \
        HRESULT hr_ = (expr);                        \
        if (FAILED(hr_)) {                           \
            Log(L"%S failed: 0x%08X", #expr, hr_);   \
            return hr_;                              \
        }                                            \
    } while (0)

// IMFAttributes, forwarded to an attribute store (IMFActivate is an IMFAttributes).
#define FORWARD_ATTRIBUTES(s)                                                                                      \
    STDMETHODIMP GetItem(REFGUID k, PROPVARIANT* v) override { return s->GetItem(k, v); }                          \
    STDMETHODIMP GetItemType(REFGUID k, MF_ATTRIBUTE_TYPE* t) override { return s->GetItemType(k, t); }            \
    STDMETHODIMP CompareItem(REFGUID k, REFPROPVARIANT v, BOOL* r) override { return s->CompareItem(k, v, r); }    \
    STDMETHODIMP Compare(IMFAttributes* a, MF_ATTRIBUTES_MATCH_TYPE m, BOOL* r) override { return s->Compare(a, m, r); } \
    STDMETHODIMP GetUINT32(REFGUID k, UINT32* v) override { return s->GetUINT32(k, v); }                          \
    STDMETHODIMP GetUINT64(REFGUID k, UINT64* v) override { return s->GetUINT64(k, v); }                          \
    STDMETHODIMP GetDouble(REFGUID k, double* v) override { return s->GetDouble(k, v); }                          \
    STDMETHODIMP GetGUID(REFGUID k, GUID* v) override { return s->GetGUID(k, v); }                                \
    STDMETHODIMP GetStringLength(REFGUID k, UINT32* n) override { return s->GetStringLength(k, n); }              \
    STDMETHODIMP GetString(REFGUID k, LPWSTR v, UINT32 n, UINT32* l) override { return s->GetString(k, v, n, l); } \
    STDMETHODIMP GetAllocatedString(REFGUID k, LPWSTR* v, UINT32* l) override { return s->GetAllocatedString(k, v, l); } \
    STDMETHODIMP GetBlobSize(REFGUID k, UINT32* n) override { return s->GetBlobSize(k, n); }                      \
    STDMETHODIMP GetBlob(REFGUID k, UINT8* b, UINT32 n, UINT32* l) override { return s->GetBlob(k, b, n, l); }    \
    STDMETHODIMP GetAllocatedBlob(REFGUID k, UINT8** b, UINT32* n) override { return s->GetAllocatedBlob(k, b, n); } \
    STDMETHODIMP GetUnknown(REFGUID k, REFIID i, LPVOID* p) override { return s->GetUnknown(k, i, p); }            \
    STDMETHODIMP SetItem(REFGUID k, REFPROPVARIANT v) override { return s->SetItem(k, v); }                        \
    STDMETHODIMP DeleteItem(REFGUID k) override { return s->DeleteItem(k); }                                       \
    STDMETHODIMP DeleteAllItems() override { return s->DeleteAllItems(); }                                         \
    STDMETHODIMP SetUINT32(REFGUID k, UINT32 v) override { return s->SetUINT32(k, v); }                            \
    STDMETHODIMP SetUINT64(REFGUID k, UINT64 v) override { return s->SetUINT64(k, v); }                            \
    STDMETHODIMP SetDouble(REFGUID k, double v) override { return s->SetDouble(k, v); }                            \
    STDMETHODIMP SetGUID(REFGUID k, REFGUID v) override { return s->SetGUID(k, v); }                               \
    STDMETHODIMP SetString(REFGUID k, LPCWSTR v) override { return s->SetString(k, v); }                           \
    STDMETHODIMP SetBlob(REFGUID k, const UINT8* b, UINT32 n) override { return s->SetBlob(k, b, n); }             \
    STDMETHODIMP SetUnknown(REFGUID k, IUnknown* u) override { return s->SetUnknown(k, u); }                       \
    STDMETHODIMP LockStore() override { return s->LockStore(); }                                                   \
    STDMETHODIMP UnlockStore() override { return s->UnlockStore(); }                                               \
    STDMETHODIMP GetCount(UINT32* n) override { return s->GetCount(n); }                                           \
    STDMETHODIMP GetItemByIndex(UINT32 i, GUID* k, PROPVARIANT* v) override { return s->GetItemByIndex(i, k, v); } \
    STDMETHODIMP CopyAllItems(IMFAttributes* d) override { return s->CopyAllItems(d); }

#define REFCOUNT                                                     \
    std::atomic<ULONG> m_ref{1};                                     \
                                                                     \
public:                                                              \
    STDMETHODIMP_(ULONG) AddRef() override { return ++m_ref; }       \
    STDMETHODIMP_(ULONG) Release() override {                        \
        ULONG n = --m_ref;                                           \
        if (!n) delete this;                                         \
        return n;                                                    \
    }

static HRESULT Answer(void** ppv, IUnknown* p) {
    *ppv = p;
    p->AddRef();
    return S_OK;
}

// ------------------------------------------------------------------ picture
// Where the webcam's picture goes in the strip: as big as fits FIT_*, centered, black bars around it.
// NV12 needs even positions and sizes.
struct Layout {
    UINT32 x = 0, y = 0, w = 0, h = 0;
};

static Layout Fit(UINT32 sw, UINT32 sh) {
    Layout l;
    if (!sw || !sh) return l;
    if ((UINT64)sw * FIT_H >= (UINT64)sh * FIT_W) {  // wider than that: full width
        l.w = FIT_W;
        l.h = (UINT32)((UINT64)FIT_W * sh / sw) & ~1u;
    } else {
        l.h = FIT_H;
        l.w = (UINT32)((UINT64)FIT_H * sw / sh) & ~1u;
    }
    l.w = std::max(l.w, 2u);
    l.h = std::max(l.h, 2u);
    l.x = ((FIT_W - l.w) / 2) & ~1u;
    l.y = ((FIT_H - l.h) / 2) & ~1u;
    return l;
}

static void Black(BYTE* frame) {
    memset(frame, 16, OUT_W * OUT_H);
    memset(frame + OUT_W * OUT_H, 128, OUT_W * OUT_H / 2);
}

// One plane, area-averaged from (sw x sh) to (dw x dh); `px` bytes per pixel (1 for Y, 2 for NV12's UV).
static void ScalePlane(const BYTE* src, LONG sstride, UINT32 sw, UINT32 sh, BYTE* dst, LONG dstride, UINT32 dw,
                       UINT32 dh, int px) {
    std::vector<UINT32> x0(dw), x1(dw);
    for (UINT32 x = 0; x < dw; ++x) {
        x0[x] = (UINT32)((UINT64)x * sw / dw);
        x1[x] = std::max(x0[x] + 1, (UINT32)((UINT64)(x + 1) * sw / dw));
    }
    for (UINT32 y = 0; y < dh; ++y) {
        UINT32 y0 = (UINT32)((UINT64)y * sh / dh), y1 = std::max(y0 + 1, (UINT32)((UINT64)(y + 1) * sh / dh));
        BYTE* out = dst + (LONGLONG)y * dstride;
        for (UINT32 x = 0; x < dw; ++x) {
            for (int c = 0; c < px; ++c) {
                UINT32 sum = 0;
                for (UINT32 yy = y0; yy < y1; ++yy) {
                    const BYTE* row = src + (LONGLONG)yy * sstride + c;
                    for (UINT32 xx = x0[x]; xx < x1[x]; ++xx) sum += row[xx * px];
                }
                UINT32 count = (y1 - y0) * (x1[x] - x0[x]);
                out[x * px + c] = (BYTE)((sum + count / 2) / count);
            }
        }
    }
}

// An NV12 picture (Y plane, then interleaved UV at half resolution) into the frame at `l`.
static void Draw(const BYTE* y, const BYTE* uv, LONG stride, UINT32 sw, UINT32 sh, const Layout& l, BYTE* frame) {
    ScalePlane(y, stride, sw, sh, frame + l.y * OUT_W + l.x, OUT_W, l.w, l.h, 1);
    ScalePlane(uv, stride, sw / 2, sh / 2, frame + OUT_W * OUT_H + (l.y / 2) * OUT_W + l.x, OUT_W, l.w / 2, l.h / 2, 2);
}

// A 16:9 test pattern instead of the webcam, while %ProgramData%\AndroidDesktop\camera-test-pattern exists
// (`python -m androiddesktop camera test`): colored quadrants (red top left, green top right, blue bottom
// left, yellow bottom right), a white arrow pointing up, a magenta border. Shows where the picture ends up.
static bool TestPattern() {
    wchar_t path[MAX_PATH];
    return ExpandEnvironmentStringsW(L"%ProgramData%\\AndroidDesktop\\camera-test-pattern", path, MAX_PATH) &&
           GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

static void DrawTestPattern(BYTE* frame) {
    const UINT32 w = 1280, h = 720;
    struct Yuv { BYTE y, u, v; };
    const Yuv red{81, 90, 240}, green{145, 54, 34}, blue{41, 240, 110}, yellow{210, 16, 146}, white{235, 128, 128},
        magenta{106, 202, 222};
    std::vector<BYTE> image(w * h * 3 / 2);
    for (UINT32 y = 0; y < h; y += 2)
        for (UINT32 x = 0; x < w; x += 2) {
            Yuv c = y < h / 2 ? (x < w / 2 ? red : green) : (x < w / 2 ? blue : yellow);
            UINT32 dx = x > w / 2 ? x - w / 2 : w / 2 - x;
            if (y >= 40 && y < 280 && dx * 2 < (y - 40)) c = white;  // the arrow's head, its tip at the top
            if (x < 16 || x >= w - 16 || y < 16 || y >= h - 16) c = magenta;
            for (UINT32 yy = y; yy < y + 2; ++yy) memset(&image[yy * w + x], c.y, 2);
            image[w * h + (y / 2) * w + x] = c.u;
            image[w * h + (y / 2) * w + x + 1] = c.v;
        }
    Black(frame);
    Draw(image.data(), image.data() + w * h, w, w, h, Fit(w, h), frame);
}

// ------------------------------------------------------------------ the PC's webcam
// The first physical webcam, read as NV12 at a native size near 1280x720 and 30 fps.
class Webcam {
public:
    ~Webcam() { Close(); }
    bool IsOpen() const { return m_reader != nullptr; }

    HRESULT Open() {
        ComPtr<IMFAttributes> query;
        CHECK(MFCreateAttributes(&query, 1));
        CHECK(query->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE, MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID));
        IMFActivate** devices = nullptr;
        UINT32 count = 0;
        CHECK(MFEnumDeviceSources(query.Get(), &devices, &count));
        ComPtr<IMFActivate> chosen;
        std::wstring name;
        for (UINT32 i = 0; i < count; ++i) {
            ComPtr<IMFActivate> device;
            device.Attach(devices[i]);
            std::wstring link = String(device.Get(), MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK);
            std::wstring friendly = String(device.Get(), MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME);
            if (!chosen && IsPhysical(link, friendly)) {
                chosen = device;
                name = friendly;
            }
        }
        CoTaskMemFree(devices);
        if (!chosen) {
            Log(L"no webcam found");
            return MF_E_NOT_FOUND;
        }
        ComPtr<IMFMediaSource> source;
        CHECK(chosen->ActivateObject(IID_PPV_ARGS(&source)));
        ComPtr<IMFAttributes> options;
        CHECK(MFCreateAttributes(&options, 1));
        CHECK(options->SetUINT32(MF_SOURCE_READER_ENABLE_ADVANCED_VIDEO_PROCESSING, TRUE));
        HRESULT hr = MFCreateSourceReaderFromMediaSource(source.Get(), options.Get(), &m_reader);
        if (FAILED(hr)) {
            source->Shutdown();
            Log(L"can't read '%s': 0x%08X", name.c_str(), hr);
            return hr;
        }
        hr = Configure();
        if (FAILED(hr)) {
            Log(L"can't configure '%s': 0x%08X", name.c_str(), hr);
            Close();
            return hr;
        }
        Log(L"reading '%s' at %ux%u", name.c_str(), m_w, m_h);
        return S_OK;
    }

    void Close() { m_reader.Reset(); }

    // The next picture, drawn into `frame` (under `lock`). S_FALSE: no new picture this time.
    HRESULT Read(BYTE* frame, std::mutex& lock) {
        DWORD index = 0, flags = 0;
        LONGLONG time = 0;
        ComPtr<IMFSample> sample;
        CHECK(m_reader->ReadSample(VIDEO, 0, &index, &flags, &time, &sample));
        if (flags & (MF_SOURCE_READERF_ERROR | MF_SOURCE_READERF_ENDOFSTREAM)) return MF_E_END_OF_STREAM;
        if (flags & MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED) CHECK(ReadFormat());
        if (!sample) return S_FALSE;
        ComPtr<IMFMediaBuffer> buffer;
        CHECK(sample->ConvertToContiguousBuffer(&buffer));
        ComPtr<IMF2DBuffer> buffer2d;
        BYTE* data = nullptr;
        LONG stride = m_stride;
        if (SUCCEEDED(buffer.As(&buffer2d)) && SUCCEEDED(buffer2d->Lock2D(&data, &stride))) {
            if (stride > 0) {
                std::lock_guard<std::mutex> guard(lock);
                Paint(data, stride, frame);
            }
            buffer2d->Unlock2D();
        } else {
            DWORD max = 0, length = 0;
            CHECK(buffer->Lock(&data, &max, &length));
            if (length >= (DWORD)stride * m_h * 3 / 2) {
                std::lock_guard<std::mutex> guard(lock);
                Paint(data, stride, frame);
            }
            buffer->Unlock();
        }
        return S_OK;
    }

private:
    static std::wstring String(IMFAttributes* a, REFGUID key) {
        WCHAR* s = nullptr;
        UINT32 n = 0;
        std::wstring out;
        if (SUCCEEDED(a->GetAllocatedString(key, &s, &n))) {
            out = s;
            CoTaskMemFree(s);
        }
        return out;
    }

    // Not this camera, nor any other virtual camera (software devices that MFCreateVirtualCamera made).
    static bool IsPhysical(const std::wstring& link, const std::wstring& friendly) {
        std::wstring upper = link;
        for (auto& c : upper) c = towupper(c);
        return friendly != FRIENDLY_NAME && upper.find(L"VCAMDEVAPI") == std::wstring::npos;
    }

    // The native format nearest 30 fps and 1280 wide (not tiny); Media Foundation converts it to NV12.
    HRESULT Configure() {
        ComPtr<IMFMediaType> best;
        LONGLONG bestScore = -1;
        for (DWORD i = 0;; ++i) {
            ComPtr<IMFMediaType> type;
            if (FAILED(m_reader->GetNativeMediaType(VIDEO, i, &type))) break;
            GUID subtype = GUID_NULL;
            UINT32 w = 0, h = 0, num = 0, den = 1;
            type->GetGUID(MF_MT_SUBTYPE, &subtype);
            MFGetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, &w, &h);
            MFGetAttributeRatio(type.Get(), MF_MT_FRAME_RATE, &num, &den);
            if (!w || !h || !den) continue;
            double fps = (double)num / den;
            LONGLONG score = (fps > 29 && fps < 31) ? 4000 : (fps >= 15 && fps <= 60) ? 2000 - (LONGLONG)(fabs(fps - 30) * 20) : 0;
            score += w >= 640 ? 2000 - std::min<LONGLONG>(2000, std::abs((LONGLONG)w - 1280)) : 0;
            score += subtype == MFVideoFormat_NV12 ? 300 : subtype == MFVideoFormat_YUY2 ? 200 : 0;
            if (score > bestScore) {
                best = type;
                bestScore = score;
            }
        }
        if (!best) return MF_E_INVALIDMEDIATYPE;
        CHECK(m_reader->SetCurrentMediaType(VIDEO, nullptr, best.Get()));
        UINT32 w = 0, h = 0, num = FPS, den = 1;
        MFGetAttributeSize(best.Get(), MF_MT_FRAME_SIZE, &w, &h);
        MFGetAttributeRatio(best.Get(), MF_MT_FRAME_RATE, &num, &den);
        ComPtr<IMFMediaType> nv12;
        CHECK(MFCreateMediaType(&nv12));
        CHECK(nv12->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video));
        CHECK(nv12->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12));
        CHECK(MFSetAttributeSize(nv12.Get(), MF_MT_FRAME_SIZE, w & ~1u, h & ~1u));
        CHECK(MFSetAttributeRatio(nv12.Get(), MF_MT_FRAME_RATE, num, den));
        CHECK(m_reader->SetCurrentMediaType(VIDEO, nullptr, nv12.Get()));
        return ReadFormat();
    }

    HRESULT ReadFormat() {
        ComPtr<IMFMediaType> type;
        CHECK(m_reader->GetCurrentMediaType(VIDEO, &type));
        CHECK(MFGetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, &m_w, &m_h));
        UINT32 stride = 0;
        m_stride = SUCCEEDED(type->GetUINT32(MF_MT_DEFAULT_STRIDE, &stride)) && (LONG)stride > 0 ? (LONG)stride : (LONG)m_w;
        m_layout = Fit(m_w, m_h);
        m_painted = false;
        return S_OK;
    }

    void Paint(const BYTE* data, LONG stride, BYTE* frame) {
        if (!m_painted) {  // new size: clear the old picture's bars
            Black(frame);
            m_painted = true;
        }
        Draw(data, data + (LONGLONG)stride * m_h, stride, m_w, m_h, m_layout, frame);
    }

    ComPtr<IMFSourceReader> m_reader;
    UINT32 m_w = 0, m_h = 0;
    LONG m_stride = 0;
    Layout m_layout;
    bool m_painted = false;
};

// ------------------------------------------------------------------ stream
class Stream final : public IMFMediaStream2 {
    REFCOUNT
    Stream() { ++g_objects; }
    ~Stream() {
        if (m_thread) CloseHandle(m_thread);
        --g_objects;
    }

    HRESULT Initialize(IMFMediaSource* source) {
        m_source = source;
        m_frame.resize(FRAME_BYTES);
        Black(m_frame.data());
        CHECK(MFCreateEventQueue(&m_queue));
        CHECK(MFCreateAttributes(&m_attributes, 4));
        CHECK(SetStreamAttributes(m_attributes.Get()));
        ComPtr<IMFMediaType> type;
        CHECK(MFCreateMediaType(&type));
        CHECK(type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video));
        CHECK(type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12));
        CHECK(type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive));
        CHECK(type->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE));
        CHECK(type->SetUINT32(MF_MT_FIXED_SIZE_SAMPLES, TRUE));
        CHECK(type->SetUINT32(MF_MT_SAMPLE_SIZE, FRAME_BYTES));
        CHECK(type->SetUINT32(MF_MT_DEFAULT_STRIDE, OUT_W));
        CHECK(MFSetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, OUT_W, OUT_H));
        CHECK(MFSetAttributeRatio(type.Get(), MF_MT_FRAME_RATE, FPS, 1));
        CHECK(MFSetAttributeRatio(type.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1));
        CHECK(type->SetUINT32(MF_MT_AVG_BITRATE, FRAME_BYTES * 8 * FPS));
        IMFMediaType* types[] = {type.Get()};
        CHECK(MFCreateStreamDescriptor(0, 1, types, &m_descriptor));
        ComPtr<IMFMediaTypeHandler> handler;
        CHECK(m_descriptor->GetMediaTypeHandler(&handler));
        CHECK(handler->SetCurrentMediaType(type.Get()));
        CHECK(SetStreamAttributes(m_descriptor.Get()));
        return S_OK;
    }

    IMFAttributes* Attributes() { return m_attributes.Get(); }
    IMFStreamDescriptor* Descriptor() { return m_descriptor.Get(); }

    void Shutdown() {
        SetStreamState(MF_STREAM_STATE_STOPPED);
        std::lock_guard<std::mutex> guard(m_lock);
        m_shutdown = true;
        m_tokens.clear();
        if (m_queue) m_queue->Shutdown();
        m_source = nullptr;
    }

    // IUnknown
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == __uuidof(IMFMediaEventGenerator) || riid == __uuidof(IMFMediaStream) ||
            riid == __uuidof(IMFMediaStream2))
            return Answer(ppv, static_cast<IMFMediaStream2*>(this));
        *ppv = nullptr;
        return E_NOINTERFACE;
    }

    // IMFMediaEventGenerator
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** e) override {
        ComPtr<IMFMediaEventQueue> q;
        CHECK(Queue(&q));
        return q->GetEvent(flags, e);
    }
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* cb, IUnknown* state) override {
        ComPtr<IMFMediaEventQueue> q;
        CHECK(Queue(&q));
        return q->BeginGetEvent(cb, state);
    }
    STDMETHODIMP EndGetEvent(IMFAsyncResult* r, IMFMediaEvent** e) override {
        ComPtr<IMFMediaEventQueue> q;
        CHECK(Queue(&q));
        return q->EndGetEvent(r, e);
    }
    STDMETHODIMP QueueEvent(MediaEventType t, REFGUID ext, HRESULT status, const PROPVARIANT* v) override {
        ComPtr<IMFMediaEventQueue> q;
        CHECK(Queue(&q));
        return q->QueueEventParamVar(t, ext, status, v);
    }

    // IMFMediaStream
    STDMETHODIMP GetMediaSource(IMFMediaSource** source) override {
        if (!source) return E_POINTER;
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown || !m_source) return MF_E_SHUTDOWN;
        return Answer((void**)source, m_source.Get());
    }
    STDMETHODIMP GetStreamDescriptor(IMFStreamDescriptor** sd) override {
        if (!sd) return E_POINTER;
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        return Answer((void**)sd, m_descriptor.Get());
    }
    // Answered with the next frame Run() sends.
    STDMETHODIMP RequestSample(IUnknown* token) override {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        if (m_state != MF_STREAM_STATE_RUNNING) return MF_E_MEDIA_SOURCE_WRONGSTATE;
        if (m_tokens.size() < 8) m_tokens.emplace_back(token);
        return S_OK;
    }

    // IMFMediaStream2
    STDMETHODIMP SetStreamState(MF_STREAM_STATE state) override {
        if (state == MF_STREAM_STATE_PAUSED) return MF_E_INVALID_STATE_TRANSITION;
        if (state != MF_STREAM_STATE_RUNNING && state != MF_STREAM_STATE_STOPPED) return E_INVALIDARG;
        HANDLE wait = nullptr;
        {
            std::lock_guard<std::mutex> guard(m_lock);
            if (m_shutdown) return MF_E_SHUTDOWN;
            if (state == m_state) return S_FALSE;
            m_state = state;
            long run = ++m_run;  // a reader thread runs while m_run is still its number
            if (state == MF_STREAM_STATE_RUNNING) {
                Reader* reader = new (std::nothrow) Reader{this, run, nullptr};
                if (reader) AddRef();  // the reader thread's
                m_thread = reader ? CreateThread(nullptr, 0, ReaderThread, reader, 0, nullptr) : nullptr;
                if (!m_thread) {
                    ++m_run;
                    m_state = MF_STREAM_STATE_STOPPED;
                    if (reader) {
                        delete reader;
                        Release();
                    }
                    return E_FAIL;
                }
            } else {
                m_tokens.clear();
                wait = m_thread;
                m_thread = nullptr;
            }
            m_queue->QueueEventParamVar(state == MF_STREAM_STATE_RUNNING ? MEStreamStarted : MEStreamStopped,
                                        GUID_NULL, S_OK, nullptr);
        }
        if (wait) {  // stopping takes a frame's time; a webcam that hangs mustn't hang Frame Server
            WaitForSingleObject(wait, 3000);
            CloseHandle(wait);
        }
        return S_OK;
    }
    STDMETHODIMP GetStreamState(MF_STREAM_STATE* state) override {
        if (!state) return E_POINTER;
        std::lock_guard<std::mutex> guard(m_lock);
        *state = m_state;
        return S_OK;
    }

private:
    static HRESULT SetStreamAttributes(IMFAttributes* a) {
        CHECK(a->SetGUID(MF_DEVICESTREAM_STREAM_CATEGORY, PIN_VIDEO_CAPTURE));
        CHECK(a->SetUINT32(MF_DEVICESTREAM_STREAM_ID, 0));
        CHECK(a->SetUINT32(MF_DEVICESTREAM_FRAMESERVER_SHARED, 1));
        CHECK(a->SetUINT32(MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES, MFFrameSourceTypes_Color));
        return S_OK;
    }

    HRESULT Queue(IMFMediaEventQueue** q) {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        *q = m_queue.Get();
        (*q)->AddRef();
        return S_OK;
    }

    struct Reader {
        Stream* stream;
        long run;
        HANDLE fresh;  // set for each new webcam picture
    };

    static DWORD WINAPI ReaderThread(void* p) {
        Reader reader = *static_cast<Reader*>(p);
        delete static_cast<Reader*>(p);
        reader.stream->Run(reader.run);
        reader.stream->Release();
        return 0;
    }

    static DWORD WINAPI WebcamThread(void* p) {
        Reader reader = *static_cast<Reader*>(p);
        delete static_cast<Reader*>(p);
        reader.stream->ReadWebcam(reader.run, reader.fresh);
        reader.stream->Release();
        return 0;
    }

    // Sends frames while the stream runs, at a steady pace from the start: each new webcam picture
    // right away, and the last one again (black before the first) when the webcam is late, as while
    // it starts. Otherwise the emulator, tired of waiting, makes up an empty frame of its own, which
    // it then fails to convert ("Could not convert from BGR4 to I420").
    void Run(long run) {
        CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        MFStartup(MF_VERSION, MFSTARTUP_LITE);
        bool pattern = TestPattern();
        Log(pattern ? L"streaming the test pattern" : L"streaming");
        {
            std::lock_guard<std::mutex> guard(m_frameLock);
            pattern ? DrawTestPattern(m_frame.data()) : Black(m_frame.data());
        }
        HANDLE fresh = CreateEventW(nullptr, FALSE, FALSE, nullptr), webcam = nullptr;
        Reader* reader = pattern || !fresh ? nullptr : new (std::nothrow) Reader{this, run, fresh};
        if (reader) {
            AddRef();  // the webcam thread's
            webcam = CreateThread(nullptr, 0, WebcamThread, reader, 0, nullptr);
            if (!webcam) {
                delete reader;
                Release();
            }
        }
        const DWORD frame = (DWORD)(FRAME_TIME / 10000);
        while (m_run == run) {
            if (fresh) WaitForSingleObject(fresh, webcam ? frame * 3 / 2 : frame);
            else Sleep(frame);
            Deliver();
        }
        if (webcam) {
            if (WaitForSingleObject(webcam, 3000) == WAIT_OBJECT_0 && fresh) CloseHandle(fresh);  // else it's still in use
            CloseHandle(webcam);
        } else if (fresh) {
            CloseHandle(fresh);
        }
        Log(L"stopped");
        MFShutdown();
        CoUninitialize();
    }

    // Reads the webcam into m_frame while the stream runs. If there's none (unplugged) or it fails,
    // the picture goes black, with another try every 2 seconds.
    void ReadWebcam(long run, HANDLE fresh) {
        CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        MFStartup(MF_VERSION, MFSTARTUP_LITE);
        {
            Webcam webcam;
            ULONGLONG retry = 0;
            while (m_run == run) {
                if (!webcam.IsOpen()) {
                    if (GetTickCount64() >= retry && FAILED(webcam.Open())) retry = GetTickCount64() + 2000;
                    if (!webcam.IsOpen()) {
                        Sleep(100);
                        continue;
                    }
                }
                HRESULT hr = webcam.Read(m_frame.data(), m_frameLock);
                if (hr == S_OK) {
                    SetEvent(fresh);
                } else if (FAILED(hr)) {
                    Log(L"webcam stopped: 0x%08X", hr);
                    webcam.Close();
                    std::lock_guard<std::mutex> guard(m_frameLock);
                    Black(m_frame.data());
                    retry = GetTickCount64() + 2000;
                }
            }
        }
        MFShutdown();
        CoUninitialize();
    }

    // The current frame, to the oldest waiting request.
    void Deliver() {
        ComPtr<IUnknown> token;
        ComPtr<IMFMediaEventQueue> queue;
        {
            std::lock_guard<std::mutex> guard(m_lock);
            if (m_shutdown || m_state != MF_STREAM_STATE_RUNNING || m_tokens.empty()) return;
            token = m_tokens.front();
            m_tokens.pop_front();
            queue = m_queue;
        }
        ComPtr<IMFMediaBuffer> buffer;
        ComPtr<IMFSample> sample;
        BYTE* data = nullptr;
        if (FAILED(MFCreateMemoryBuffer(FRAME_BYTES, &buffer)) || FAILED(buffer->Lock(&data, nullptr, nullptr))) return;
        {
            std::lock_guard<std::mutex> guard(m_frameLock);
            memcpy(data, m_frame.data(), FRAME_BYTES);
        }
        buffer->Unlock();
        buffer->SetCurrentLength(FRAME_BYTES);
        if (FAILED(MFCreateSample(&sample)) || FAILED(sample->AddBuffer(buffer.Get()))) return;
        sample->SetSampleTime(MFGetSystemTime());
        sample->SetSampleDuration(FRAME_TIME);
        if (token) sample->SetUnknown(MFSampleExtension_Token, token.Get());
        queue->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample.Get());
    }

    std::mutex m_lock, m_frameLock;
    ComPtr<IMFMediaEventQueue> m_queue;
    ComPtr<IMFAttributes> m_attributes;
    ComPtr<IMFStreamDescriptor> m_descriptor;
    ComPtr<IMFMediaSource> m_source;  // dropped at Shutdown, which breaks the cycle
    MF_STREAM_STATE m_state = MF_STREAM_STATE_STOPPED;
    bool m_shutdown = false;
    std::deque<ComPtr<IUnknown>> m_tokens;
    std::vector<BYTE> m_frame;
    std::atomic<long> m_run{0};
    HANDLE m_thread = nullptr;
};

// ------------------------------------------------------------------ source
class Source final : public IMFMediaSourceEx, public IMFGetService, public IKsControl {
    REFCOUNT
    Source() {
        ++g_objects;
        MFStartup(MF_VERSION, MFSTARTUP_LITE);
    }
    ~Source() {
        MFShutdown();
        --g_objects;
    }

    HRESULT Initialize(IMFAttributes* given) {
        CHECK(MFCreateEventQueue(&m_queue));
        CHECK(MFCreateAttributes(&m_attributes, 8));
        if (given) given->CopyAllItems(m_attributes.Get());
        m_stream.Attach(new (std::nothrow) Stream());
        if (!m_stream) return E_OUTOFMEMORY;
        CHECK(m_stream->Initialize(this));
        IMFStreamDescriptor* sds[] = {m_stream->Descriptor()};
        CHECK(MFCreatePresentationDescriptor(1, sds, &m_pd));
        CHECK(m_pd->SelectStream(0));
        return S_OK;
    }

    // IUnknown
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == __uuidof(IMFMediaEventGenerator) || riid == __uuidof(IMFMediaSource) ||
            riid == __uuidof(IMFMediaSourceEx))
            return Answer(ppv, static_cast<IMFMediaSourceEx*>(this));
        if (riid == __uuidof(IMFGetService)) return Answer(ppv, static_cast<IMFGetService*>(this));
        if (riid == __uuidof(IKsControl)) return Answer(ppv, static_cast<IKsControl*>(this));
        *ppv = nullptr;
        return E_NOINTERFACE;
    }

    // IMFMediaEventGenerator
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** e) override {
        ComPtr<IMFMediaEventQueue> q;
        CHECK(Queue(&q));
        return q->GetEvent(flags, e);
    }
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* cb, IUnknown* state) override {
        ComPtr<IMFMediaEventQueue> q;
        CHECK(Queue(&q));
        return q->BeginGetEvent(cb, state);
    }
    STDMETHODIMP EndGetEvent(IMFAsyncResult* r, IMFMediaEvent** e) override {
        ComPtr<IMFMediaEventQueue> q;
        CHECK(Queue(&q));
        return q->EndGetEvent(r, e);
    }
    STDMETHODIMP QueueEvent(MediaEventType t, REFGUID ext, HRESULT status, const PROPVARIANT* v) override {
        ComPtr<IMFMediaEventQueue> q;
        CHECK(Queue(&q));
        return q->QueueEventParamVar(t, ext, status, v);
    }

    // IMFMediaSource
    STDMETHODIMP GetCharacteristics(DWORD* c) override {
        if (!c) return E_POINTER;
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        *c = MFMEDIASOURCE_IS_LIVE;
        return S_OK;
    }
    STDMETHODIMP CreatePresentationDescriptor(IMFPresentationDescriptor** pd) override {
        if (!pd) return E_POINTER;
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        return m_pd->Clone(pd);
    }
    STDMETHODIMP Start(IMFPresentationDescriptor* pd, const GUID* format, const PROPVARIANT* start) override {
        if (!pd || !start) return E_INVALIDARG;
        if (format && *format != GUID_NULL) return MF_E_UNSUPPORTED_TIME_FORMAT;
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        DWORD count = 0;
        CHECK(pd->GetStreamDescriptorCount(&count));
        for (DWORD i = 0; i < count; ++i) {
            BOOL selected = FALSE;
            ComPtr<IMFStreamDescriptor> sd;
            DWORD id = 0;
            CHECK(pd->GetStreamDescriptorByIndex(i, &selected, &sd));
            CHECK(sd->GetStreamIdentifier(&id));
            if (id != 0) return MF_E_INVALIDSTREAMNUMBER;
            if (selected) {
                CHECK(m_pd->SelectStream(0));
                HRESULT hr = m_stream->SetStreamState(MF_STREAM_STATE_RUNNING);
                CHECK(hr);
                if (hr == S_FALSE)  // already running: a restart still answers with MEStreamStarted
                    CHECK(m_stream->QueueEvent(MEStreamStarted, GUID_NULL, S_OK, nullptr));
                CHECK(m_queue->QueueEventParamUnk(m_announced ? MEUpdatedStream : MENewStream, GUID_NULL, S_OK,
                                                  static_cast<IMFMediaStream2*>(m_stream.Get())));
                m_announced = true;
            } else {
                CHECK(m_pd->DeselectStream(0));
                CHECK(m_stream->SetStreamState(MF_STREAM_STATE_STOPPED));
            }
        }
        Log(L"started (build " __DATE__ L" " __TIME__ L")");
        return m_queue->QueueEventParamVar(MESourceStarted, GUID_NULL, S_OK, start);
    }
    STDMETHODIMP Stop() override {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        m_stream->SetStreamState(MF_STREAM_STATE_STOPPED);
        return m_queue->QueueEventParamVar(MESourceStopped, GUID_NULL, S_OK, nullptr);
    }
    STDMETHODIMP Pause() override { return MF_E_INVALID_STATE_TRANSITION; }
    STDMETHODIMP Shutdown() override {
        ComPtr<Stream> stream;
        {
            std::lock_guard<std::mutex> guard(m_lock);
            if (m_shutdown) return MF_E_SHUTDOWN;
            m_shutdown = true;
            m_queue->Shutdown();
            stream = m_stream;
        }
        stream->Shutdown();
        return S_OK;
    }

    // IMFMediaSourceEx
    STDMETHODIMP GetSourceAttributes(IMFAttributes** a) override {
        if (!a) return E_POINTER;
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        return Answer((void**)a, m_attributes.Get());
    }
    STDMETHODIMP GetStreamAttributes(DWORD id, IMFAttributes** a) override {
        if (!a) return E_POINTER;
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        if (id != 0) return MF_E_INVALIDSTREAMNUMBER;
        return Answer((void**)a, m_stream->Attributes());
    }
    STDMETHODIMP SetD3DManager(IUnknown*) override { return S_OK; }  // our samples are in system memory

    // IMFGetService
    STDMETHODIMP GetService(REFGUID, REFIID, LPVOID* ppv) override {
        if (ppv) *ppv = nullptr;
        return MF_E_UNSUPPORTED_SERVICE;
    }

    // IKsControl: no camera controls (exposure, zoom...).
    STDMETHODIMP KsProperty(PKSPROPERTY, ULONG, LPVOID, ULONG, ULONG*) override { return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND); }
    STDMETHODIMP KsMethod(PKSMETHOD, ULONG, LPVOID, ULONG, ULONG*) override { return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND); }
    STDMETHODIMP KsEvent(PKSEVENT, ULONG, LPVOID, ULONG, ULONG*) override { return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND); }

private:
    HRESULT Queue(IMFMediaEventQueue** q) {
        std::lock_guard<std::mutex> guard(m_lock);
        if (m_shutdown) return MF_E_SHUTDOWN;
        *q = m_queue.Get();
        (*q)->AddRef();
        return S_OK;
    }

    std::mutex m_lock;
    ComPtr<IMFMediaEventQueue> m_queue;
    ComPtr<IMFAttributes> m_attributes;
    ComPtr<IMFPresentationDescriptor> m_pd;
    ComPtr<Stream> m_stream;
    bool m_shutdown = false, m_announced = false;
};

// ------------------------------------------------------------------ activation
// What Frame Server creates from our CLSID; it hands out a new media source each time.
class Activator final : public IMFActivate {
    REFCOUNT
    Activator() { ++g_objects; }
    ~Activator() { --g_objects; }
    HRESULT Initialize() { return MFCreateAttributes(&m_attributes, 4); }

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == __uuidof(IMFAttributes) || riid == __uuidof(IMFActivate))
            return Answer(ppv, static_cast<IMFActivate*>(this));
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    FORWARD_ATTRIBUTES(m_attributes)

    STDMETHODIMP ActivateObject(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        *ppv = nullptr;
        ComPtr<Source> source;
        source.Attach(new (std::nothrow) Source());
        if (!source) return E_OUTOFMEMORY;
        CHECK(source->Initialize(m_attributes.Get()));
        return source->QueryInterface(riid, ppv);
    }
    STDMETHODIMP ShutdownObject() override { return S_OK; }
    STDMETHODIMP DetachObject() override { return E_NOTIMPL; }

private:
    ComPtr<IMFAttributes> m_attributes;
};

class Factory final : public IClassFactory {
public:
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IClassFactory) return Answer(ppv, static_cast<IClassFactory*>(this));
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return 2; }  // static
    STDMETHODIMP_(ULONG) Release() override { return 1; }
    STDMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        *ppv = nullptr;
        if (outer) return CLASS_E_NOAGGREGATION;
        ComPtr<Activator> activator;
        activator.Attach(new (std::nothrow) Activator());
        if (!activator) return E_OUTOFMEMORY;
        CHECK(activator->Initialize());
        return activator->QueryInterface(riid, ppv);
    }
    STDMETHODIMP LockServer(BOOL lock) override {
        lock ? ++g_locks : --g_locks;
        return S_OK;
    }
};

static Factory g_factory;

// ------------------------------------------------------------------ the virtual camera itself
// Created for every user (needs an administrator) and kept until removed, across restarts. Runs on its
// own thread: regsvr32's is a single-threaded apartment.
struct CameraJob {
    bool remove;
    HRESULT hr;
};

static DWORD WINAPI CameraThread(void* p) {
    CameraJob* job = static_cast<CameraJob*>(p);
    job->hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(job->hr)) return 0;
    job->hr = MFStartup(MF_VERSION, MFSTARTUP_LITE);
    if (SUCCEEDED(job->hr)) {
        ComPtr<IMFVirtualCamera> camera;
        job->hr = MFCreateVirtualCamera(MFVirtualCameraType_SoftwareCameraSource, MFVirtualCameraLifetime_System,
                                        MFVirtualCameraAccess_AllUsers, FRIENDLY_NAME, CLSID_STRING, nullptr, 0,
                                        &camera);
        if (SUCCEEDED(job->hr)) {
            job->hr = job->remove ? camera->Remove() : camera->Start(nullptr);
            camera->Shutdown();
        }
        Log(L"%s the camera: 0x%08X", job->remove ? L"removing" : L"creating", job->hr);
        MFShutdown();
    }
    CoUninitialize();
    return 0;
}

static HRESULT Camera(bool remove) {
    CameraJob job = {remove, E_FAIL};
    HANDLE thread = CreateThread(nullptr, 0, CameraThread, &job, 0, nullptr);
    if (!thread) return HRESULT_FROM_WIN32(GetLastError());
    WaitForSingleObject(thread, INFINITE);
    CloseHandle(thread);
    return job.hr;
}

static HRESULT SetValue(HKEY key, const wchar_t* name, const wchar_t* value) {
    return HRESULT_FROM_WIN32(RegSetValueExW(key, name, 0, REG_SZ, (const BYTE*)value,
                                             (DWORD)((wcslen(value) + 1) * sizeof(wchar_t))));
}

static std::wstring ClassKey() { return std::wstring(L"SOFTWARE\\Classes\\CLSID\\") + CLSID_STRING; }

// ------------------------------------------------------------------ exports
BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = module;
        DisableThreadLibraryCalls(module);
    }
    return TRUE;
}

STDAPI DllGetClassObject(REFCLSID clsid, REFIID riid, LPVOID* ppv) {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (clsid != CLSID_AndroidDesktopCamera) return CLASS_E_CLASSNOTAVAILABLE;
    return g_factory.QueryInterface(riid, ppv);
}

STDAPI DllCanUnloadNow() { return g_objects == 0 && g_locks == 0 ? S_OK : S_FALSE; }

// Registers the media source (for Frame Server, which only reads HKLM) and creates the camera.
STDAPI DllRegisterServer() {
    wchar_t path[MAX_PATH];
    if (!GetModuleFileNameW(g_module, path, MAX_PATH)) return HRESULT_FROM_WIN32(GetLastError());
    HKEY key = nullptr, server = nullptr;
    HRESULT hr = HRESULT_FROM_WIN32(RegCreateKeyExW(HKEY_LOCAL_MACHINE, ClassKey().c_str(), 0, nullptr, 0,
                                                    KEY_WRITE, nullptr, &key, nullptr));
    if (SUCCEEDED(hr)) hr = SetValue(key, nullptr, FRIENDLY_NAME);
    if (SUCCEEDED(hr))
        hr = HRESULT_FROM_WIN32(RegCreateKeyExW(key, L"InprocServer32", 0, nullptr, 0, KEY_WRITE, nullptr, &server, nullptr));
    if (SUCCEEDED(hr)) hr = SetValue(server, nullptr, path);
    if (SUCCEEDED(hr)) hr = SetValue(server, L"ThreadingModel", L"Both");
    if (server) RegCloseKey(server);
    if (key) RegCloseKey(key);
    if (FAILED(hr)) return hr;
    return Camera(false);
}

STDAPI DllUnregisterServer() {
    HRESULT camera = Camera(true);
    LSTATUS s = RegDeleteTreeW(HKEY_LOCAL_MACHINE, ClassKey().c_str());
    if (s != ERROR_SUCCESS && s != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(s);
    return camera;
}
