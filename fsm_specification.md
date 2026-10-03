```mermaid
stateDiagram-v2
    [*] --> WaitingForPlayers
    
    state WaitingForPlayers {
        [*] --> AwaitClient1
        AwaitClient1 --> AwaitClient2: CONNECT (Player 1) / Send LOBBY_WAIT
        AwaitClient2 --> BothConnected: CONNECT (Player 2)
    }

    WaitingForPlayers --> GameSetup: Both Players Ready
    WaitingForPlayers --> GameEnded: DISCONNECT / Timeout

    state GameSetup {
        [*] --> InitializeGame
        InitializeGame --> StartBroadcast: GAME_START (Broadcast Word Lengths & Random Turn)
    }

    GameSetup --> PlayerTurn

    state PlayerTurn {
        [*] --> AwaitMove
        
        AwaitMove --> ProcessMove: MOVE (Active Player Guess)
        
        state ProcessMove {
            [*] --> ValidateMove
            
            ValidateMove --> SendError: Invalid Move / Out of Turn
            SendError --> AwaitMove: ERROR (To Active Player)
            
            ValidateMove --> EvaluateGuess: Valid MOVE
            
            state EvaluateGuess {
                [*] --> CheckLetter: Letter Guess
                [*] --> CheckWord: Word Guess
                
                CheckLetter --> UpdateState: Process Letter Result
                CheckWord --> UpdateState: Process Word Result
            }
        }

        ProcessMove --> BroadcastUpdate: Valid Guess Evaluated
        BroadcastUpdate --> AwaitMove: STATE_UPDATE (Broadcast Masked Word & Guesses)
    }

    PlayerTurn --> CheckGameCondition
    
    state CheckGameCondition <<choice>>
    CheckGameCondition --> GameEnded: Word Guessed OR Guesses Exhausted
    CheckGameCondition --> SwitchTurn: Game Continues
    
    SwitchTurn --> PlayerTurn: Alternate Active Player

    %% Global Interrupt
    PlayerTurn --> GameEnded: DISCONNECT / Sudden Dropped Connection

    state GameEnded {
        [*] --> BroadcastOutcome
        BroadcastOutcome --> [*]: GAME_OVER (Broadcast Winner / No Winner / Forfeit)
    }
~~~
