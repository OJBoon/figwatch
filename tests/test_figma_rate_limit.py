"""Tests for Figma tiered rate limiting."""

import pytest

from figwatch.providers.figma import (
    TIER_1, TIER_2, TIER_3,
    FigmaRateLimiter, endpoint_tier,
)


class TestEndpointTier:
    """Verify endpoint-to-tier mapping matches Figma docs."""

    # Tier 1: full file, nodes, image renders
    @pytest.mark.parametrize('path', [
        '/files/abc123?depth=2',
        '/files/abc123',
        '/files/abc123/nodes?ids=1%3A2&depth=100',
        '/images/abc123?ids=1%3A2&scale=1&format=png',
    ])
    def test_tier_1(self, path):
        assert endpoint_tier(path) == TIER_1

    # Tier 2: comments, dev_resources, variables, projects
    @pytest.mark.parametrize('path', [
        '/files/abc123/comments',
        '/files/abc123/dev_resources?node_ids=1%3A2',
        '/files/abc123/variables/local',
        '/files/abc123/variables/published',
        '/teams/12345/projects',
        '/projects/67890/files',
    ])
    def test_tier_2(self, path):
        assert endpoint_tier(path) == TIER_2

    # Tier 3: styles, components, metadata
    @pytest.mark.parametrize('path', [
        '/files/abc123/styles',
        '/files/abc123/components',
        '/files/abc123/component_sets',
        '/teams/12345/components',
        '/files/abc123/meta',
    ])
    def test_tier_3(self, path):
        assert endpoint_tier(path) == TIER_3

    def test_unknown_defaults_to_tier_2(self):
        assert endpoint_tier('/some/unknown/endpoint') == TIER_2


class TestFigmaRateLimiter:
    """Verify limiter routes to correct tier bucket."""

    def test_valid_plans(self):
        FigmaRateLimiter(plan='starter')
        FigmaRateLimiter(plan='professional', seat='dev')
        FigmaRateLimiter(plan='professional', seat='view')
        FigmaRateLimiter(plan='organization', seat='dev')
        FigmaRateLimiter(plan='enterprise', seat='dev')

    def test_invalid_plan_raises(self):
        with pytest.raises(ValueError):
            FigmaRateLimiter(plan='free', seat='dev')

    def test_starter_ignores_seat(self):
        # Starter has no seat distinction — should work without seat param
        limiter = FigmaRateLimiter(plan='starter')
        assert limiter._buckets[TIER_1] is not None

    def test_acquire_routes_to_correct_bucket(self):
        limiter = FigmaRateLimiter(plan='professional', seat='dev')
        # All buckets start full — acquire should succeed immediately
        limiter.acquire('/images/abc?ids=1%3A2&scale=1&format=png')  # Tier 1
        limiter.acquire('/files/abc/comments')  # Tier 2
        limiter.acquire('/files/abc/styles')  # Tier 3

    def test_professional_dev_capacities(self):
        limiter = FigmaRateLimiter(plan='professional', seat='dev')
        assert limiter._buckets[TIER_1]._capacity == 10
        assert limiter._buckets[TIER_2]._capacity == 25
        assert limiter._buckets[TIER_3]._capacity == 50

    def test_organization_dev_capacities(self):
        limiter = FigmaRateLimiter(plan='organization', seat='dev')
        assert limiter._buckets[TIER_1]._capacity == 15
        assert limiter._buckets[TIER_2]._capacity == 50
        assert limiter._buckets[TIER_3]._capacity == 100

    def test_backoff_drains_correct_tier(self):
        limiter = FigmaRateLimiter(plan='professional', seat='dev')
        # Tier 1 bucket starts full (10 tokens)
        bucket = limiter._buckets[TIER_1]
        assert bucket._tokens == 10.0

        # Backoff for 5 seconds should drain tokens negative
        limiter.backoff('/images/abc?ids=1%3A2', 5.0)
        assert bucket._tokens < 0
