"""Exercise the phone's real playlist reader with paged YouTube transport responses."""

import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

NODE_READER = r"""
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const [count, importing] = JSON.parse(process.argv[1]);
const requested = [];
const list = 'PL1234567890123456789';
const context = vm.createContext({
    Capacitor: {
        isPluginAvailable: name => name === 'CapacitorHttp',
        nativePromise: async (plugin, method, options) => {
            if (plugin !== 'CapacitorHttp' || method !== 'request'
                || !options.url.startsWith('https://music.youtube.com/youtubei/v1/browse?')) {
                throw new Error('Unexpected transport request');
            }
            const payload = options.data;
            requested.push(payload.continuation || payload.browseId);
            const page = payload.continuation ? Number(payload.continuation.slice(4)) : 0;
            const start = page * 100;
            const items = Array.from({ length: Math.min(100, count - start) }, (_, i) => ({
                musicResponsiveListItemRenderer: {
                    playlistItemData: { videoId: String(start + i).padStart(11, '0') },
                    flexColumns: [{ musicResponsiveListItemFlexColumnRenderer: {
                        text: { runs: [{ text: `Song ${start + i}` }] }
                    } }]
                }
            }));
            if (start + 100 < count) items.push({ continuationItemRenderer: {
                continuationEndpoint: { continuationCommand: { token: `page${page + 1}` } }
            } });
            const data = page ? {
                onResponseReceivedActions: [{ appendContinuationItemsAction: { continuationItems: items } }]
            } : {
                contents: { twoColumnBrowseResultsRenderer: {
                    secondaryContents: { sectionListRenderer: {
                        contents: [{ musicPlaylistShelfRenderer: { contents: items } }]
                    } },
                    tabs: [{ tabRenderer: { content: { sectionListRenderer: {
                        contents: [{ musicResponsiveHeaderRenderer: {
                            title: { runs: [{ text: 'Paged playlist' }] }
                        } }]
                    } } } }]
                } }
            };
            return { status: 200, data };
        }
    }
});

// Keep the playlist reader, catalog parser, and pagination real. Only the
// device preferences/player and network are substituted; no browser is needed.
const stubs = {
    'state.js': { STORAGE: {} },
    'prefs.js': { pref: () => 'US' },
    'native-engine.js': { nativeAvailable: () => false, nativeSearch: async () => [] }
};
const modules = new Map();
function load(file) {
    if (modules.has(file)) return modules.get(file);
    const stub = stubs[path.basename(file)];
    const module = stub
        ? new vm.SyntheticModule(Object.keys(stub), function () {
            for (const [key, value] of Object.entries(stub)) this.setExport(key, value);
        }, { context, identifier: file })
        : new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), { context, identifier: file });
    modules.set(file, module);
    return module;
}
const file = importing ? 'playlist-links.js' : 'ytmusic.js';
const reader = load(path.resolve('pwa/src', file));
await reader.link((specifier, parent) => load(path.resolve(path.dirname(parent.identifier), specifier)));
await reader.evaluate();
const result = importing
    ? await reader.namespace.readPlaylistLink({ url: `https://music.youtube.com/playlist?list=${list}` })
    : await reader.namespace.albumTracks({ album_id: `VL${list}` });
console.log(JSON.stringify({
    ids: result.tracks.map(track => track.source_id),
    partial: result.partial,
    total: result.total,
    requested
}));
"""


@pytest.mark.parametrize(
    "count, importing, kept, partial, requests",
    [
        (50, True, 50, False, 1),
        (500, True, 500, False, 5),
        (2000, True, 2000, False, 20),
        (2500, True, 2000, True, 20),
        (500, False, 400, True, 4),
    ],
    ids=["short-import", "full-import", "exact-ceiling", "partial-import", "bounded-browse"],
)
def test_playlist_import_reads_all_pages_and_reports_the_ceiling(count, importing, kept, partial, requests):
    completed = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-e", NODE_READER,
         json.dumps([count, importing])],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["ids"] == [str(i).zfill(11) for i in range(kept)]
    assert result["partial"] is partial
    assert len(result["requested"]) == requests
    assert result["requested"][0] == "VLPL1234567890123456789"
    if importing:
        assert result["total"] == kept
