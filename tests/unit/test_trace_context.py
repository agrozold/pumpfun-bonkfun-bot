"""Unit tests for TraceContext"""
import pytest
import time
from src.analytics.trace_context import (
    TraceContext, 
    TraceEvent,
    get_current_trace, 
    get_trace_id
)


class TestTraceEvent:
    """Tests for TraceEvent"""
    
    def test_event_creation(self):
        event = TraceEvent.now('t0_signal', source='test')
        assert event.stage == 't0_signal'
        assert event.data == {'source': 'test'}
        assert event.timestamp_mono > 0
        assert 'Z' in event.timestamp_wall


class TestTraceContext:
    """Tests for TraceContext"""
    
    def test_context_creation(self):
        ctx = TraceContext.start(
            trade_type='buy',
            mint='TestMint123',
            source='pumpportal',
            slot_detected=12345
        )
        
        assert ctx.trace_id is not None
        assert len(ctx.trace_id) == 12
        assert ctx.trade_type == 'buy'
        assert ctx.mint == 'TestMint123'
        assert ctx.t0 is not None
        assert len(ctx.events) == 1
        
        ctx.finish()
    
    def test_context_var_binding(self):
        ctx = TraceContext.start('buy', 'TestMint', 'test')
        
        assert get_current_trace() == ctx
        assert get_trace_id() == ctx.trace_id
        
        ctx.finish()
        
        assert get_current_trace() is None
        assert get_trace_id() is None
    
    def test_latency_calculation(self):
        ctx = TraceContext.start('buy', 'TestMint', 'test')
        
        time.sleep(0.01)
        ctx.mark_build_complete(accounts=5)
        
        time.sleep(0.01)
        ctx.mark_sent(provider='helius', signature='sig123')
        
        time.sleep(0.01)
        ctx.mark_finalized(slot_landed=12350, success=True)
        
        assert ctx.total_latency_ms is not None
        assert ctx.total_latency_ms >= 30
        assert ctx.build_latency_ms >= 10
        assert ctx.send_latency_ms >= 10
        assert ctx.confirm_latency_ms >= 10
        
        ctx.finish()
    
    def test_serialization(self):
        ctx = TraceContext.start('sell', 'Mint456', 'shredstream', slot_detected=100)
        ctx.mark_build_complete()
        ctx.mark_sent(provider='jito', signature='sig789')
        ctx.mark_finalized(slot_landed=105, success=True)
        
        data = ctx.to_dict()
        
        assert data['trace_id'] == ctx.trace_id
        assert data['trade_type'] == 'sell'
        assert data['outcome'] == 'success'
        assert len(data['events']) == 4
        assert data['latency']['total_ms'] is not None
        
        ctx.finish()
    
    def test_failed_transaction(self):
        ctx = TraceContext.start('buy', 'FailMint', 'test')
        ctx.mark_build_complete()
        ctx.mark_sent(provider='helius', signature='sig_fail')
        ctx.mark_finalized(success=False, fail_reason='InsufficientFunds')
        
        assert ctx.outcome == 'fail'
        assert ctx.fail_reason == 'InsufficientFunds'
        
        ctx.finish()
