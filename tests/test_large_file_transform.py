"""run_large_file_transform must not quietly corrupt a document.

It exists to correct OCR across an 840-page legal corpus, where the output is far
too long for anyone to read end to end. Every failure mode here is therefore a
*silent* one: a segment dropped, segments reordered by concurrency, a blank line
inserted at every boundary, or a chunk the model summarised instead of correcting.

No network: `route` is monkeypatched, so this runs offline and for free.

Run: python3 tests/test_large_file_transform.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_executor as ex  # noqa: E402

# Long enough to force several chunks at the sizes used below.
DOC = ''.join(f'line {i:04d} some polish-ish text zazolc gesla jazn\n' for i in range(600))


class _Route:
    """Stand-in for agent_router.route with controllable behaviour."""

    def __init__(self, mode='echo', bad_segment=None):
        self.mode = mode
        self.bad_segment = bad_segment
        self.calls = []

    def __call__(self, model, prompt, max_tokens, **kw):
        # The segment payload is what sits between the SEGMENT header and the
        # trailing instruction block.
        body = prompt.split('---\n', 1)[1].rsplit('\n\nReturn ONLY', 1)[0]
        idx = int(prompt.split('--- SEGMENT ', 1)[1].split(' of ', 1)[0])
        self.calls.append((idx, kw.get('caller'), kw.get('project_path'), kw.get('policy_path')))
        if self.mode == 'truncate' and idx == self.bad_segment:
            return body[:len(body) // 10], 10, 5, 0.001      # model summarised/was cut off
        if self.mode == 'raise' and idx == self.bad_segment:
            raise RuntimeError('provider exploded')
        if self.mode == 'drop_lines' and idx == self.bad_segment:
            # Reproduces what scw-mistral-small-24b actually did to the OCR corpus:
            # ~10% of lines removed, the rest padded so total length barely moves.
            lines = body.split('\n')
            kept = [l for j, l in enumerate(lines) if j % 10]
            return '\n'.join(x + ' xxxxx' for x in kept), 10, 10, 0.001
        return body, 10, 10, 0.001


def _run(route, **kw):
    orig = ex.route
    ex.route = route
    try:
        return ex.run_large_file_transform(
            'scw-qwen3.6-35b', 'Correct the OCR.', DOC, 8192, '/tmp/project', **kw)
    finally:
        ex.route = orig


def test_roundtrip_is_byte_identical():
    """An identity transform must reproduce the document exactly.

    This is the check that catches a join that inserts newlines between segments,
    which would corrupt every boundary in an 840-page file.
    """
    text, _ti, _to, _c, report = _run(_Route())
    assert report['chunks'] > 1, 'test document did not chunk; raise its size'
    assert text == DOC, (
        f'document changed: {len(text)} chars out vs {len(DOC)} in')


def test_concurrency_does_not_reorder():
    """Same input at different concurrency must give byte-identical output."""
    serial, *_ = _run(_Route(), concurrency=1)
    parallel, *_ = _run(_Route(), concurrency=8)
    assert serial == parallel, 'output differs between concurrency 1 and 8'
    assert serial == DOC


def test_short_output_is_flagged():
    """A segment that comes back far shorter must be reported, not accepted."""
    text, _ti, _to, _c, report = _run(_Route('truncate', bad_segment=2))
    assert report['suspect'], 'a 10%-length segment was not flagged'
    assert report['suspect'][0]['segment'] == 2
    assert text != DOC, 'truncated content should differ from the source'


def test_dropped_lines_are_flagged_even_at_full_length():
    """Lines lost while total length holds must still be caught.

    Not hypothetical: on the real OCR corpus scw-mistral-small-24b returned a
    character-length ratio of 0.985 — inside the length tolerance — while dropping
    142 of 1 480 lines and reordering table rows. A character count cannot see
    loss spread thinly across a segment; a line count can.
    """
    text, _ti, _to, _c, report = _run(_Route('drop_lines', bad_segment=1))
    assert report['suspect'], 'a segment missing 10% of its lines was not flagged'
    assert report['suspect'][0]['reason'] == 'lines dropped'
    # And the length check alone would indeed have let it through.
    s = report['suspect'][0]
    assert s['out_chars'] >= s['in_chars'] * 0.7, (
        'test no longer exercises the length-passes-but-lines-lost case')


def test_failed_segment_keeps_the_original():
    """A provider error must not punch a hole in the middle of the document."""
    text, _ti, _to, _c, report = _run(_Route('raise', bad_segment=2))
    assert report['failed'] and report['failed'][0]['segment'] == 2
    assert text == DOC, 'failed segment did not fall back to the original text'


def test_tools_are_disabled_and_eu_policy_preserved():
    """Transformation is not an agentic task: tools off, EU boundary still resolved."""
    r = _Route()
    _run(r)
    assert r.calls, 'no calls made'
    for idx, caller, project_path, policy_path in r.calls:
        assert project_path is None, f'segment {idx}: tools enabled via project_path'
        assert policy_path == '/tmp/project', f'segment {idx}: EU policy path lost'
        assert caller.endswith(':map')


def test_chunks_are_sized_for_the_output_cap():
    """Chunks must fit what the model can emit, not what it can read.

    The batch path chunks at 280k chars because a summary returns small. Here
    output is the constraint, so a chunk must be well under out_max tokens.
    """
    _t, _ti, _to, _c, report = _run(_Route())
    assert report['chunk_chars'] <= 8192 * 3.2, 'chunk larger than the output cap allows'
    assert report['chunk_chars'] >= 2000, 'chunk absurdly small'


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith('test_') or not callable(fn):
            continue
        try:
            fn()
            print(f'  ok    {name}')
        except AssertionError as e:
            failures += 1
            print(f'  FAIL  {name}\n          {e}')
    print()
    print('ALL PASS' if not failures else f'{failures} FAILURE(S)')
    raise SystemExit(1 if failures else 0)
