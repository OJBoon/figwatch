"""Tests for the feedback module."""

import json
import os
import tempfile
from unittest import mock

import pytest

from figwatch.feedback import (
    build_feedback_url,
    parse_feedback_params,
    render_form,
    save_feedback,
)


class TestBuildFeedbackUrl:
    def test_basic_url(self):
        url = build_feedback_url(
            'https://figwatch.example.com',
            audit_id='abc123',
            skill='builtin:ux',
            attempt=1,
            trace_id='',
        )
        assert url.startswith('https://figwatch.example.com/feedback?')
        assert 'audit_id=abc123' in url
        assert 'skill=builtin%3Aux' in url
        assert 'attempt=1' in url
        assert 'trace_id' not in url

    def test_with_trace_id(self):
        url = build_feedback_url(
            'https://figwatch.example.com',
            audit_id='abc123',
            skill='builtin:ux',
            attempt=2,
            trace_id='deadbeef',
        )
        assert 'trace_id=deadbeef' in url
        assert 'attempt=2' in url

    def test_trailing_slash_stripped(self):
        url = build_feedback_url(
            'https://figwatch.example.com/',
            audit_id='x',
            skill='s',
            attempt=1,
            trace_id='',
        )
        assert '//feedback' not in url
        assert '/feedback?' in url


class TestParseFeedbackParams:
    def test_extracts_params(self):
        params = parse_feedback_params(
            '/feedback?audit_id=abc&skill=builtin%3Aux&attempt=1&trace_id=dead'
        )
        assert params == {
            'audit_id': 'abc',
            'skill': 'builtin:ux',
            'attempt': '1',
            'trace_id': 'dead',
        }

    def test_empty_path(self):
        assert parse_feedback_params('/feedback') == {}


class TestRenderForm:
    def test_contains_hidden_fields(self):
        html = render_form({
            'audit_id': 'abc',
            'skill': 'builtin:ux',
            'attempt': '2',
            'trace_id': 'dead',
        })
        assert 'value="abc"' in html
        assert 'value="builtin:ux"' in html
        assert 'value="2"' in html
        assert 'value="dead"' in html
        assert 'name="rating"' in html
        assert 'name="comment"' in html

    def test_escapes_html(self):
        html = render_form({'audit_id': '<script>alert(1)</script>'})
        assert '<script>' not in html
        assert '&lt;script&gt;' in html


class TestSaveFeedback:
    def test_saves_and_appends(self, tmp_path):
        feedback_file = tmp_path / '.feedback.json'
        with mock.patch('figwatch.feedback._FEEDBACK_FILE', feedback_file):
            save_feedback('a1', 'skill1', '1', 'trace1', 4, 'good')
            save_feedback('a2', 'skill2', '2', 'trace2', 5, 'great')

        data = json.loads(feedback_file.read_text())
        assert len(data) == 2
        assert data[0]['audit_id'] == 'a1'
        assert data[0]['rating'] == 4
        assert data[0]['comment'] == 'good'
        assert data[1]['audit_id'] == 'a2'
        assert data[1]['rating'] == 5
        assert 'submitted_at' in data[0]
