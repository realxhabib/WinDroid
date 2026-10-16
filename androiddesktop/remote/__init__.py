"""Android apps on an iPhone or iPad, over the home Wi-Fi, in Safari.

hub.py decides which app is on which device; mirror.py streams an app's Android display (scrcpy-server)
and takes input for it; server.py is the LAN-only web server the devices connect to; pairing.py pairs
devices and checks every connection; certs.py makes the https:// certificates; h264.py decodes video on
the PC for devices that get JPEG frames; web/ is the web app itself.
"""
