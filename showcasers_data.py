"""Static showcaser/YouTube channel data for the homepage.

Previously the homepage called /api/find-channels on every load, which scraped
YouTube live. YouTube intermittently throttles Render's outbound IP and stalls
the connection for 60s+, causing the homepage (and all concurrent requests on
the single-threaded dev server) to hang and 503.

We now serve the channel info statically here. It only needs updating when a
showcaser changes. The live /api/find-channels endpoint remains available but is
no longer on the critical homepage path.
"""

SHOWCASERS = [
    {
        "name": "MastersMZ",
        "handle": "@MastersMZ",
        "url": "https://www.youtube.com/@MastersMZ",
        "pfp_url": "https://yt3.googleusercontent.com/YhVMgeFMm9GKsG1NZEO5YzPg8zvClDc0jdHcB1dq0ilD62iwEZWhOZwxC8u9KMiq6kHMlUWpFA=s900-c-k-c0x00ffffff-no-rj",
        "found": True,
    },
    {
        "name": "1F0YT",
        "handle": "@1F0YT",
        "url": "https://www.youtube.com/@1F0YT",
        "pfp_url": None,
        "found": True,
    },
    {
        "name": "ReverseScripts",
        "handle": "@ReverseScripts",
        "url": "https://www.youtube.com/@ReverseScripts",
        "pfp_url": None,
        "found": True,
    },
    {
        "name": "Sr. LDS",
        "handle": "@Sr_LDS",
        "url": "https://www.youtube.com/@Sr_LDS",
        "pfp_url": "https://yt3.googleusercontent.com/P5I7IgZm_pjDFckluDqw8sybkg3km0eEFK86reTJmJGAYoRlU0c_vJoxb2WLZuQZPvIaBJTblqM=s900-c-k-c0x00ffffff-no-rj",
        "found": True,
    },
    {
        "name": "Ras Scripts",
        "handle": "@rasscript",
        "url": "https://www.youtube.com/@rasscript",
        "pfp_url": "https://yt3.googleusercontent.com/_ZVVjU81mqFl-e79JT_DPELVNZ97Uf2LZ43EXsH8sSwzy_-tcudld4sQMHRdUk68aaxbkH3RzQ=s900-c-k-c0x00ffffff-no-rj",
        "found": True,
    },
]
