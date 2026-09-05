"""Tests for Hermes Model Resilience & Fallback Engine (Phase H.3 Part B)."""

import unittest
from unittest.mock import MagicMock, patch

from agent.error_classifier import FailoverReason, classify_api_error
from hermes_cli.fallback_config import get_fallback_chain


class TestModelResilienceClassification(unittest.TestCase):
    """Tests classification of eligible vs ineligible failover errors."""

    def test_eligible_rate_limit_429(self):
        class Mock429(Exception):
            status_code = 429
            message = "Rate limit exceeded"

        res = classify_api_error(Mock429("Rate limit exceeded"))
        self.assertIn(res.reason, {FailoverReason.rate_limit, FailoverReason.upstream_rate_limit})

    def test_eligible_billing_402(self):
        class Mock402(Exception):
            status_code = 402
            message = "Insufficient quota or credits"

        res = classify_api_error(Mock402("Insufficient quota"))
        self.assertEqual(res.reason, FailoverReason.billing)

    def test_eligible_overloaded_503(self):
        class Mock503(Exception):
            status_code = 503
            message = "Service Unavailable / overloaded"

        res = classify_api_error(Mock503("Service Unavailable"))
        self.assertIn(res.reason, {FailoverReason.overloaded, FailoverReason.server_error})

    def test_eligible_connection_timeout(self):
        res = classify_api_error(TimeoutError("Connection timed out"))
        self.assertEqual(res.reason, FailoverReason.timeout)

    def test_ineligible_context_overflow_not_failover(self):
        """Context overflow must trigger compression, NOT provider failover."""
        class MockContextOverflow(Exception):
            status_code = 400
            message = "maximum context length is 8192 tokens, but request has 9500 tokens"

        res = classify_api_error(MockContextOverflow("context length exceeded"))
        self.assertEqual(res.reason, FailoverReason.context_overflow)
        # Context overflow is handled by compression, not failover


class TestModelFallbackChain(unittest.TestCase):
    """Tests fallback chain ordering and stepping semantics."""

    def test_fallback_chain_order(self):
        cfg = {
            "fallback_providers": [
                {"provider": "opencode-free", "model": "laguna-s-2.1-free"},
                {"provider": "opencode-free", "model": "deepseek-v4-flash-free"},
            ]
        }
        chain = get_fallback_chain(cfg)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0]["provider"], "opencode-free")
        self.assertEqual(chain[0]["model"], "laguna-s-2.1-free")
        self.assertEqual(chain[1]["provider"], "opencode-free")
        self.assertEqual(chain[1]["model"], "deepseek-v4-flash-free")

    def test_fallback_activation_progression(self):
        """Test step-by-step activation across the chain until exhaustion."""
        from agent.chat_completion_helpers import try_activate_fallback

        mock_agent = MagicMock()
        mock_agent._fallback_chain = [
            {"provider": "opencode-free", "model": "laguna-s-2.1-free"},
            {"provider": "opencode-free", "model": "deepseek-v4-flash-free"},
        ]
        mock_agent._fallback_index = 0
        mock_agent._fallback_activated = False
        mock_agent._rate_limit_backoff_count = 0
        mock_agent._unavailable_fallback_keys = set()
        mock_agent.provider = "commandcode"
        mock_agent.model = "z-ai/glm-5.3-flash"
        mock_agent._primary_runtime = {"provider": "commandcode", "model": "z-ai/glm-5.3-flash"}

        with patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None), \
             patch("agent.backend_identity.should_skip_candidate", return_value=False), \
             patch("agent.auxiliary_client.resolve_provider_client", return_value=(MagicMock(), "fallback-model")):
            # Step 1: primary fails -> fallback #1
            activated = try_activate_fallback(mock_agent, FailoverReason.rate_limit)
            self.assertTrue(activated)
            self.assertEqual(mock_agent._fallback_index, 1)

            # Step 2: fallback #1 fails -> fallback #2
            activated = try_activate_fallback(mock_agent, FailoverReason.overloaded)
            self.assertTrue(activated)
            self.assertEqual(mock_agent._fallback_index, 2)

            # Step 3: fallback #2 fails -> chain exhausted
            activated = try_activate_fallback(mock_agent, FailoverReason.server_error)
            self.assertFalse(activated)


if __name__ == "__main__":
    unittest.main()
