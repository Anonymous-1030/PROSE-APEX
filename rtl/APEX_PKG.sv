//=============================================================================
// APEX Design Parameters Package
//
// Centralizes all architectural constants shared across the APEX scoring
// pipeline and CEFE front-end modules. Modules import apex_pkg::* to
// guarantee consistent parameterization across the hierarchy.
//
// Target: ASAP7 7nm @ 1 GHz
//=============================================================================
`timescale 1ns/1ps

package apex_pkg;

    // --- Scoring Datapath ---
    parameter int SCORE_W     = 16;   // Score bit-width (fixed-point Q0.16)
    parameter int ID_W        = 9;    // Chunk ID width (512 entries)
    parameter int NUM_EXPERTS = 7;    // Expert prediction banks
    parameter int K_ENTRIES   = 25;   // Top-K retained entries
    parameter int EZ_SIZE     = 7;    // Eviction zone: min-heap depth 2
    parameter int SZ_SIZE     = 18;   // Safe zone: flat register array

    // --- CEFE Front-End ---
    parameter int NUM_VC      = 16;   // Virtual channels (one per host)
    parameter int QUEUE_DEPTH = 32;   // Per-VC queue depth

    // --- Derived Constants ---
    parameter int NUM_CHUNKS  = 1 << ID_W;  // 512 chunk address space
    parameter int EZ_DEPTH    = 2;    // Min-heap levels in eviction zone

endpackage
