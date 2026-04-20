"""Tests for figwatch.ports — repository protocols and Figma implementations."""

from unittest.mock import patch

import pytest

from figwatch.ports import CommentRepository, DesignDataRepository
from figwatch.providers.figma import FigmaCommentRepository, FigmaDesignDataRepository


# ── Protocol conformance ─────────────────────────────────────────────

def test_figma_comment_repo_satisfies_protocol():
    assert isinstance(FigmaCommentRepository('pat'), CommentRepository)


def test_figma_design_data_repo_satisfies_protocol():
    assert isinstance(FigmaDesignDataRepository('pat'), DesignDataRepository)


# ── FigmaCommentRepository ───────────────────────────────────────────

@patch('figwatch.providers.figma.figma_post')
def test_post_reply_returns_id(mock_post):
    mock_post.return_value = {'id': 'comment-42'}
    repo = FigmaCommentRepository('test-pat')
    result = repo.post_reply('file-1', 'parent-1', 'hello')
    assert result == 'comment-42'
    mock_post.assert_called_once_with(
        '/files/file-1/comments',
        {'message': 'hello', 'comment_id': 'parent-1'},
        'test-pat',
    )


@patch('figwatch.providers.figma.figma_post')
def test_post_reply_raises_on_error(mock_post):
    mock_post.side_effect = Exception('network error')
    repo = FigmaCommentRepository('test-pat')
    with pytest.raises(Exception, match='network error'):
        repo.post_reply('f', 'p', 'msg')


@patch('figwatch.providers.figma.figma_delete')
def test_delete_comment_calls_api(mock_delete):
    repo = FigmaCommentRepository('test-pat')
    repo.delete_comment('file-1', 'comment-1')
    mock_delete.assert_called_once_with('/files/file-1/comments/comment-1', 'test-pat')


@patch('figwatch.providers.figma.figma_delete')
def test_delete_comment_silent_on_error(mock_delete):
    mock_delete.side_effect = Exception('gone')
    repo = FigmaCommentRepository('test-pat')
    repo.delete_comment('f', 'c')  # should not raise


@patch('figwatch.providers.figma.figma_get')
def test_fetch_comments(mock_get):
    mock_get.return_value = {'comments': [{'id': '1'}, {'id': '2'}]}
    repo = FigmaCommentRepository('test-pat')
    result = repo.fetch_comments('file-1')
    assert len(result) == 2


@patch('figwatch.providers.figma.figma_get')
def test_fetch_comments_empty_on_none(mock_get):
    mock_get.return_value = None
    repo = FigmaCommentRepository('test-pat')
    assert repo.fetch_comments('f') == []


# ── FigmaDesignDataRepository ────────────────────────────────────────

@patch('figwatch.providers.figma.fetch_figma_data')
def test_design_data_repo_delegates(mock_fetch):
    mock_fetch.return_value = ({'screenshot': '/tmp/s.png'}, {'name': 'Frame'})
    repo = FigmaDesignDataRepository('test-pat')
    data, tree = repo.fetch(['screenshot'], 'file-1', '2:3')
    assert data == {'screenshot': '/tmp/s.png'}
    assert tree == {'name': 'Frame'}
    mock_fetch.assert_called_once_with(['screenshot'], 'file-1', '2:3', 'test-pat', limiter=None)
