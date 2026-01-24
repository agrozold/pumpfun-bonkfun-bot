"""Unit tests for PositionState machine"""
import pytest
from src.trading.position_state import (
    PositionState,
    StateMachine,
    InvalidStateTransitionError,
    VALID_TRANSITIONS
)


class TestPositionState:
    """Tests for PositionState enum"""
    
    def test_all_states_exist(self):
        states = [
            'PENDING_BUY', 'OPENING', 'OPEN', 
            'PARTIALLY_FILLED', 'PENDING_SELL', 
            'CLOSING', 'CLOSED', 'FAILED'
        ]
        for state in states:
            assert hasattr(PositionState, state)
    
    def test_valid_transitions_defined(self):
        for state in PositionState:
            assert state in VALID_TRANSITIONS


class TestStateMachine:
    """Tests for StateMachine"""
    
    def test_initial_state(self):
        sm = StateMachine(current_state=PositionState.PENDING_BUY)
        assert sm.current_state == PositionState.PENDING_BUY
        assert len(sm.history) == 1
        assert sm.history[0].reason == 'initial'
    
    def test_valid_transition(self):
        sm = StateMachine(current_state=PositionState.PENDING_BUY)
        
        sm.transition_to(PositionState.OPENING, reason='buy confirmed')
        assert sm.current_state == PositionState.OPENING
        assert len(sm.history) == 2
        
        sm.transition_to(PositionState.OPEN, reason='initialized')
        assert sm.current_state == PositionState.OPEN
    
    def test_invalid_transition_raises(self):
        sm = StateMachine(current_state=PositionState.PENDING_BUY)
        
        with pytest.raises(InvalidStateTransitionError):
            sm.transition_to(PositionState.CLOSED)
    
    def test_force_transition(self):
        sm = StateMachine(current_state=PositionState.PENDING_BUY)
        
        # Force allows invalid transitions
        sm.transition_to(PositionState.CLOSED, reason='forced', force=True)
        assert sm.current_state == PositionState.CLOSED
    
    def test_is_final(self):
        sm_closed = StateMachine(current_state=PositionState.CLOSED)
        sm_failed = StateMachine(current_state=PositionState.FAILED)
        sm_open = StateMachine(current_state=PositionState.OPEN)
        
        assert sm_closed.is_final is True
        assert sm_failed.is_final is True
        assert sm_open.is_final is False
    
    def test_is_active(self):
        active_states = [
            PositionState.PENDING_BUY,
            PositionState.OPENING,
            PositionState.OPEN,
            PositionState.PARTIALLY_FILLED,
            PositionState.PENDING_SELL,
            PositionState.CLOSING
        ]
        
        for state in active_states:
            sm = StateMachine(current_state=state)
            assert sm.is_active is True
        
        sm_closed = StateMachine(current_state=PositionState.CLOSED)
        assert sm_closed.is_active is False
    
    def test_serialization_roundtrip(self):
        sm = StateMachine(current_state=PositionState.PENDING_BUY)
        sm.transition_to(PositionState.OPENING, reason='confirmed', trace_id='abc123')
        sm.transition_to(PositionState.OPEN, reason='ready')
        
        data = sm.to_dict()
        restored = StateMachine.from_dict(data)
        
        assert restored.current_state == sm.current_state
        assert len(restored.history) == len(sm.history)
    
    def test_migration_from_is_active(self):
        # Old format with is_active
        old_data = {'is_active': True}
        sm = StateMachine.from_dict(old_data)
        assert sm.current_state == PositionState.OPEN
        
        old_data_inactive = {'is_active': False}
        sm_inactive = StateMachine.from_dict(old_data_inactive)
        assert sm_inactive.current_state == PositionState.CLOSED
