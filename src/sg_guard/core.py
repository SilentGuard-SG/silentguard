"""Signed token scores, rolling windows, and deterministic character deletion."""
import copy
import hashlib
import math


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def score_return(text, token_scores, offsets, threshold=None, width=16):
    """Score every stride-one window. No clean counterpart or attack label is used."""
    if not isinstance(text, str) or type(width) is not int or width < 1:
        raise ValueError("Expected text and a positive integer window width")
    q = [float(x) for x in token_scores]
    if len(q) != len(offsets) or not all(math.isfinite(x) for x in q):
        raise ValueError("Nonfinite scores or score/offset length mismatch")
    if threshold is not None and not math.isfinite(float(threshold)):
        raise ValueError("Nonfinite threshold")
    previous = (-1, -1)
    for pair in offsets:
        if len(pair) != 2 or any(type(x) is not int for x in pair):
            raise ValueError("Offsets must be integer pairs")
        a, b = pair
        if not (0 <= a < b <= len(text)) or a < previous[0] or b < previous[1]:
            raise ValueError("Offsets outside the tool return or out of order")
        previous = (a, b)
    result = dict(text_sha256=text_hash(text), requested_width=width, n_B=len(q),
                  threshold=threshold, score=None, alert=None, spans=[],
                  window_scores=[], flagged_window_starts=[])
    if not q:
        return dict(result, status="empty_return", effective_width=0)
    k = min(width, len(q))
    means = [math.fsum(q[i:i+k]) / k for i in range(len(q)-k+1)]
    peak = max(range(len(means)), key=means.__getitem__)
    starts = [] if threshold is None else [i for i, s in enumerate(means) if s > threshold]
    spans = []
    for i in starts:
        a, b = offsets[i][0], offsets[i+k-1][1]
        if spans and a <= spans[-1]["end"]:
            spans[-1]["end"] = max(b, spans[-1]["end"])
            spans[-1]["peak_score"] = max(means[i], spans[-1]["peak_score"])
        else:
            spans.append(dict(start=a, end=b, peak_score=means[i]))
    return dict(result, status="scored", effective_width=k, score=means[peak],
                peak_token_start=peak, peak_char_span=[offsets[peak][0], offsets[peak+k-1][1]],
                alert=None if threshold is None else bool(starts), spans=spans,
                window_scores=means, flagged_window_starts=starts)


def edit_return(text, decision):
    """Delete flagged interval unions from the exact text that was scored."""
    if text_hash(text) != decision["text_sha256"]:
        raise ValueError("Decision belongs to another tool return")
    if decision["threshold"] is None:
        raise ValueError("An uncalibrated score cannot edit a return")
    previous_end = 0
    for span in decision["spans"]:
        a, b = span["start"], span["end"]
        if type(a) is not int or type(b) is not int or not 0 <= previous_end <= a < b <= len(text):
            raise ValueError("Malformed or overlapping character spans")
        previous_end = b
    result = text
    edits = []
    for span in reversed(decision["spans"]):
        a, b = span["start"], span["end"]
        result = result[:a] + result[b:]
        edits.append(dict(original_char_span=[a, b], peak_score=span["peak_score"],
                          level="fixed", replacement=""))
    edits.reverse()
    return result, dict(mode="delete", input_sha256=text_hash(text), output_sha256=text_hash(result),
                        threshold=decision["threshold"], strong_threshold=None,
                        edits=edits, changed=result != text,
                        placement="model-visible tool content; flagged unions removed",
                        requires_fresh_prefill=result != text,
                        copied_untrusted_text_into_trusted_message=False)


def edit_request(request, decision):
    """Return a copy. Only the final tool message's content can change."""
    result = copy.deepcopy(request)
    if not result.get("messages") or result["messages"][-1]["role"] != "tool":
        raise ValueError("Expected the current tool-return boundary")
    changed, receipt = edit_return(result["messages"][-1]["content"], decision)
    result["messages"][-1]["content"] = changed
    return result, receipt
