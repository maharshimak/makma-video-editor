# Architecture and implemented scope

Next.js editor -> authenticated FastAPI -> SQLAlchemy project/version/job state -> local media storage and FFmpeg render worker; a provider router supplies optional AI planning.

Timeline operations (move/trim/split/transition), viewport/indexing, compositor preview, immutable project revisions and undo/redo, asset analysis/proxies/waveforms, job progress/SSE, AI director planning and usage tracking.

## Modules inspected

- `tools/fetch-ffmpeg.py`: already_available, fetch, extract, main.
- `tests/conftest.py`: pytest_configure, media_dir, _run, sample_video, silent_video, sample_audio, sample_image, out_path, app_client, client, shared_project, uploaded.
- `tests/test_ai_safety.py`: ScriptedProvider, facts, plan, op, codes, test_invalid_json_is_a_problem_not_an_exception, test_prose_around_the_json_is_recovered, test_an_empty_response_is_rejected, test_a_json_array_is_not_a_plan, test_operations_must_be_present_and_a_list, test_an_operation_that_is_not_an_object_is_rejected, test_an_invented_operation_never_reaches_execution, test_an_invented_effect_is_rejected.
- `tests/test_ai_usage.py`: FakeBase, FakeBase, fresh_table, set_override, test_a_dated_model_id_inherits_its_family_price, test_the_longest_matching_prefix_wins, test_input_and_output_are_priced_separately, test_an_unknown_model_is_unknown_not_free, test_cache_reads_are_never_free_without_a_published_rate, test_a_published_cache_rate_is_used, test_inline_json_overrides_a_built_in_price, test_a_json_file_overrides_a_built_in_price, test_an_override_adds_models_without_removing_the_defaults, test_an_unusable_override_falls_back_rather_than_crashing.
- `tests/test_api_e2e.py`: client, drain_jobs, project, _remember_sample, uploaded, test_system_capabilities_are_honest, test_ingest_produces_proxy_thumbnail_and_scene_index, test_signed_media_url_is_required, test_media_supports_range_requests, test_add_clip_to_timeline, test_ai_director_plans_and_applies_the_brief_command, test_plan_is_inspectable_and_every_op_is_described.
- `tests/test_concurrency.py`: make_project, read, add_text, test_the_project_reports_a_version_that_advances_with_each_edit, test_an_edit_returns_the_version_it_produced, test_a_stale_write_is_refused_rather_than_overwriting, test_a_conflict_hands_back_the_current_document, test_retrying_against_the_new_version_succeeds, test_omitting_the_version_keeps_last_write_wins, test_undo_after_a_concurrent_edit_walks_real_history, test_a_job_is_claimed_by_exactly_one_worker, test_several_renders_queue_without_corrupting_each_other.
- `tests/test_editing.py`: make_doc, ctx, op, clips_of, test_add_clip_appends_after_the_last_clip, test_add_clip_with_unknown_asset_fails_clearly, test_image_clip_gets_a_sensible_default_duration, test_delete_with_ripple_closes_the_gap, test_trim_head_advances_the_source_in_point, test_trim_head_cannot_run_past_the_start_of_the_source, test_trim_to_nothing_is_refused, test_split_divides_the_clip_and_keeps_the_source_continuous.
- `tests/test_health.py`: doc, facts, codes, test_a_clean_project_is_ready_to_export, test_an_empty_sequence_blocks_export, test_media_that_is_gone_is_an_error_with_its_position, test_media_that_failed_to_decode_is_an_error, test_upscaling_is_flagged_but_does_not_block, test_a_clip_running_past_its_source_is_flagged, test_text_outside_the_safe_area_is_flagged, test_a_gap_on_the_bottom_track_is_reported_as_information, test_a_silent_timeline_is_mentioned.
- `tests/test_llm_live.py`: _has_credentials, project, direct, test_the_model_returns_a_usable_plan, test_every_operation_the_model_returns_is_one_this_build_can_run, test_the_model_references_only_real_ids, test_no_parameter_carries_a_path_or_a_command, test_a_slow_motion_request_produces_a_speed_change, test_a_reframe_request_changes_the_format, test_an_export_request_becomes_an_export_not_an_operation, test_a_capability_this_build_lacks_is_declared_not_faked, test_an_instruction_with_nothing_to_do_does_not_invent_an_edit.
- `tests/test_mvp_commands.py`: client, drain, project, say, document, clips, only_video, select_video, test_remove_the_first_seconds, test_split_this_here, test_put_photo_after_this_shot, test_keep_this_photo_for_three_seconds.
- `tests/test_pixels.py`: sample_frame, pixel, mean_luma, render_clip, test_letterbox_paints_bars_top_and_bottom, test_letterbox_pillarboxes_a_narrow_target, test_black_and_white_grade_removes_colour, test_exposure_actually_changes_brightness, test_opacity_composites_against_the_canvas, test_fade_in_starts_black, test_text_layer_puts_pixels_on_screen, render_keyframed.
- `tests/test_planner.py`: context, video, types, params_for, test_parse_time, test_find_range, test_cinematic_is_a_package_not_just_a_lut, test_grade_vocabulary, test_subtle_reduces_grade_intensity, test_a_quoted_title_does_not_drive_the_look, test_cut_the_first_n_seconds, test_remove_an_explicit_range.
- `tests/test_render_engine.py`: source_for, render, small_sequence, test_single_clip_renders_with_correct_duration, test_trim_takes_the_requested_source_window, test_gap_between_clips_becomes_real_timeline_time, test_speed_change_alters_clip_length, test_multi_track_composite_and_transform, test_effects_chain_renders, test_every_registered_video_effect_compiles, test_every_registered_audio_effect_compiles, test_text_layer_renders.
- `tests/test_security.py`: test_production_refuses_to_start_with_development_defaults, test_each_unsafe_default_is_caught_on_its_own, test_a_correctly_configured_production_instance_starts, test_development_never_refuses_to_start, test_a_signature_is_bound_to_one_key, test_a_signature_is_bound_to_its_expiry, test_an_expired_link_stops_working, test_a_forged_signature_is_rejected, test_production_links_are_short_lived, test_storage_rejects_traversal, test_storage_rejects_absolute_and_unc_paths, test_a_legitimate_key_still_resolves.
- `scripts/make_benchmark_project.py`: run, synthesise_media, build, main.
- `apps/web/next-env.d.ts`: source/module definitions inspected.
- `apps/web/next.config.mjs`: source/module definitions inspected.
- `apps/web/postcss.config.mjs`: source/module definitions inspected.
- `apps/web/tailwind.config.ts`: source/module definitions inspected.
- `apps/web/vitest.bench.config.ts`: source/module definitions inspected.
- `apps/web/vitest.config.ts`: source/module definitions inspected.
- `apps/web/tests/unit/timeline.test.ts`: clip, track, next.
- `apps/web/tests/bench/timeline.bench.ts`: makeClips, next, makeTracks.
- `apps/web/store/editor.ts`: getPlayhead, setPlayhead, subscribePlayhead, isPlaying, setPlaying, subscribePlaying, cloneDocument, withSequence, findClip, sequenceDuration, clipsAt, snapTargets.
- `apps/web/lib/api.ts`: getToken, setToken, request, problems, detail, watchJob, finish, cleanup, startPolling.
- `apps/web/lib/compositor.ts`: num, filterFor, scaleFilter, ease, sampleCurve, transformAt, transitionAlpha, x, top.
- `apps/web/lib/time.ts`: formatTimecode, pad, formatDuration, formatBytes, snapToFrame, rulerStep.
- `apps/web/lib/timeline.ts`: timeToX, xToTime, clampZoom, zoomAbout, visibleTimeWindow, windowIsEmpty, buildClipIndex, firstClipIndexAfter, mid, clipsInWindow, ticksInWindow, atTimeInWindow.
- `apps/web/lib/types.ts`: source/module definitions inspected.
- `apps/web/lib/useFileDrop.tsx`: useFileDrop, onDragEnter, onDragOver, onDragLeave, onDrop, DropOverlay.
- `apps/web/lib/useViewport.ts`: same, useViewport, measure, schedule.
- `apps/web/components/CommandPalette.tsx`: score, CommandPalette, onKey, runActive.
- `apps/web/components/DirectorPanel.tsx`: DirectorPanel, applyPlan, MessageBubble, PlanCard, IconCheck.
- `apps/web/components/ExportDialog.tsx`: ExportDialog, start, PreFlight, JobRow.
- `apps/web/components/Inspector.tsx`: Inspector, patchClip, PanelShell, Section, Row, NumberField, delta, AnimatableSlider, write, toggleAnimation, toggleKeyHere, rest.
- `apps/web/components/Keyframes.tsx`: curveOf, localTime, keyframeAt, valueNow, writeKeyframe, KeyframeToggle, Diamond, KeyframeStrip, at.
- `apps/web/components/MediaPanel.tsx`: MediaPanel, removeAsset, addToTimeline, addScene, AssetRow.
- `apps/web/components/Preview.tsx`: Preview, frame, elapsed, step, onKey, seek, move, up, IconPlay, IconPause, IconPrev, IconNext.
- `apps/web/components/Scopes.tsx`: Scopes, tick, draw, graticule, drawWaveform, i, luma, drawParade, paintBins, drawVectorscope, skin, drawHistogram.
- `apps/web/components/Timeline.tsx`: Timeline, onRulerPointerDown, move, up, beginDrag, onMove, deltaSeconds, onUp, onWheel, setTrackFlag, TimelineToolbar, split.
- `apps/web/components/Toasts.tsx`: Toasts.
- `apps/web/components/TopBar.tsx`: TopBar, commitName, IconUndo, IconRedo.
- `apps/web/app/layout.tsx`: RootLayout.
- `apps/web/app/page.tsx`: ProjectsPage, create, Mark, SystemBadge, Dot.
- `apps/web/app/editor/[projectId]/page.tsx`: EditorPage, onKey, Divider, move, up, clamp.
- `apps/api/makma/config.py`: UnsafeConfiguration, Settings, _discover, get_settings, model_post_init, is_production, is_test, unsafe_settings, enforce_production_safety, storage_root, tmp_root, cors_list.
- `apps/api/makma/db.py`: Base, _make_engine, init_db, _add_missing_columns, _column_ddl, get_db, session_scope, _sqlite_pragmas.
- `apps/api/makma/editing.py`: OperationError, AssetInfo, EditContext, resolve_scope, _filter_kinds, _num, _pick_track, _free_start, handler, _add_clip, _delete_clip, _move_clip, _trim_clip, _split_clip, _duplicate_clip.
- `apps/api/makma/health.py`: Issue, HealthReport, AssetFacts, check_sequence, check_project, to_dict, ready_to_export, to_dict.
- `apps/api/makma/main.py`: _install_usage_sink, lifespan, security_headers, unhandled, root, sink.
- `apps/api/makma/models.py`: User, Project, ProjectVersion, Asset, Job, ChatMessage, ModelUsage, _uid, utcnow.
- `apps/api/makma/versions.py`: VersionConflict, load_document, save_version, _trim, _version_at, can_undo, can_redo, undo, redo, restore, history, __init__.
- `apps/api/makma/__init__.py`: source/module definitions inspected.
- `apps/api/makma/storage/base.py`: StorageKey, Storage, original, proxy, audio_proxy, thumbnail, waveform, render, write_stream, write_bytes, read_bytes, exists, delete, delete_prefix.
- `apps/api/makma/storage/local.py`: LocalStorage, default_ttl, sign_key, verify_key, get_storage, __init__, _path, write_stream, write_bytes, read_bytes, exists, delete, delete_prefix.
- `apps/api/makma/storage/__init__.py`: source/module definitions inspected.
- `apps/api/makma/search/__init__.py`: source/module definitions inspected.
- `apps/api/makma/schema/core.py`: TrackKind, ClipKind, Easing, TransitionType, FitMode, TimeRange, Keyframe, Transform, Effect, Transition, TextStyle, TextContent, Clip, Track, Marker, Sequence, StyleProfile, ProjectDocument, new_id, _ease, default_sequence, duration, animated, value_at, end, source_duration, _validate, sorted_clips, duration, video_tracks.
- `apps/api/makma/schema/export.py`: export.
- `apps/api/makma/schema/media.py`: AssetKind, AssetStatus, VideoStreamInfo, AudioStreamInfo, ProbeResult, Scene, BeatAnalysis, MediaAnalysis, AssetRef, primary_video, primary_audio, duration, usable.
- `apps/api/makma/schema/operations.py`: OpType, Scope, EditOperation, CostEstimate, ExportRequest, EditPlan, OperationResult, ApplyResult, implemented, ok.
- `apps/api/makma/schema/__init__.py`: source/module definitions inspected.
- `apps/api/makma/render/compiler.py`: AssetSource, RenderSettings, CompiledRender, CompileError, SequenceCompiler, _atempo_chain, _hex_to_ffmpeg, compile_sequence, cleanup, __init__, _label, _warn, _temp_text_file, compile, _add_clip_input, _composite_clip, _lut_paths.
- `apps/api/makma/render/effects.py`: EffectSpec, EffectContext, evaluate_keyframes, _ease_python, keyframe_expression, register, _num, _build_exposure, _build_color, _build_temperature, _build_balance, _build_curves, _build_lut, _build_hue.
- `apps/api/makma/render/fonts.py`: _index, resolve_font, available_families.
- `apps/api/makma/render/renderer.py`: ExportPreset, RenderOutcome, _noop, preset_settings, resolve_assets, render_sequence, estimate_render.
- `apps/api/makma/render/scratch.py`: is_filter_safe, _candidate_roots, scratch_dir, write_text_asset, stage_readable.
- `apps/api/makma/render/__init__.py`: source/module definitions inspected.
- `apps/api/makma/media/analyze.py`: detect_scene_cuts, build_scenes, sample_frames, _frame_stats, _frame_diff, annotate_scenes, detect_silence, measure_loudness, build_waveform, rank_highlights, _highlight_reason, analyze_video.
- `apps/api/makma/media/beats.py`: BeatGrid, _decode_mono, _hann, _fft, spectral_flux, pick_onsets, estimate_tempo, tempo_prior, grid_fit, fit_phase, analyze_beats, nearest_beat, to_dict.
- `apps/api/makma/media/ffmpeg.py`: FFmpegError, Input, Output, FFmpegCommand, FilterChain, run_ffmpeg, run_ffprobe, ffmpeg_available, escape_filter_value, escape_drawtext, quote_filter_arg, escape_path_for_filter, __init__, argv, argv, add_input, add_output.
- `apps/api/makma/media/ingest.py`: UnreadableMedia, IngestResult, _noop, checksum_file, _even, build_proxy, build_audio_proxy, build_thumbnail, build_filmstrip, ingest_file, _write_waveform, _write_scene_thumbnails.
- `apps/api/makma/media/probe.py`: _fraction, _rotation, probe, classify.
- `apps/api/makma/media/__init__.py`: source/module definitions inspected.
- `apps/api/makma/jobs/handlers.py`: _fail_asset, _ffmpeg_reason, handle_ingest, handle_analyze, _render_job, handle_render, handle_export.
- `apps/api/makma/jobs/queue.py`: JobStatus, JobCancelled, JobContext, register, enqueue, claim_next, run_job, _finish, cancel, _as_naive_utc, requeue_stalled, progress, log, set_status, check_cancelled.
- `apps/api/makma/jobs/worker.py`: worker_name, _loop, start_inline_workers, stop_inline_workers, main, _handle_signal.
- `apps/api/makma/jobs/__init__.py`: source/module definitions inspected.
- `apps/api/makma/api/deps.py`: hash_password, verify_password, create_token, decode_token, _dev_user, get_current_user, get_project.
- `apps/api/makma/api/routes_ai.py`: _assets, _briefs, _infos, _reply_text, chat, apply, messages, clear_messages, usage, lines.
- `apps/api/makma/api/routes_assets.py`: _safe_filename, _asset_out, list_assets, upload_asset, get_asset, asset_usage, delete_asset, reanalyze, _range_response, serve_media, _iter.
- `apps/api/makma/api/routes_jobs.py`: _job_out, _queue_render, render, export, render_estimate, project_health, list_jobs, get_job, cancel_job, stream_job, events.
- `apps/api/makma/api/routes_projects.py`: _iso, _user_out, _project_out, _conflict, _asset_infos, signup, signin, me, update_preferences, list_projects, create_project, get_one.
- `apps/api/makma/api/routes_system.py`: capabilities, effects, health.
- `apps/api/makma/api/schemas.py`: SignUpRequest, SignInRequest, TokenResponse, UserOut, ProjectCreate, ProjectSummary, ProjectOut, ProjectUpdate, DocumentUpdate, AssetOut, ChatRequest, ChatResponse, ApplyPlanRequest, ApplyOperationsRequest, ApplyResponse, ChatMessageOut, RenderRequest, JobOut, UsageLine, UsageReport, VersionOut.
- `apps/api/makma/api/__init__.py`: source/module definitions inspected.
- `apps/api/makma/ai/context.py`: AssetBrief, _round, summarise_sequence, summarise_asset, build_context, context_size.
- `apps/api/makma/ai/director.py`: operation_schema, plan_tool, system_prompt, repair_prompt, _plan_from_result, build_plan_context, plan_edit.
- `apps/api/makma/ai/heuristic.py`: MediaRef, PlanContext, parse_time, find_duration, find_range, find_multiplier, split_clauses, strip_quoted, find_quoted, find_title, rule, op, _grade, _cinematic_package.
- `apps/api/makma/ai/pricing.py`: Price, _parse, table, reset, price_for, estimate.
- `apps/api/makma/ai/router.py`: Capability, QualityMode, Usage, ModelResponse, NotConfigured, Provider, ModelRouter, get_router, reset_router, available, describe, reason, vision, embed, transcribe, __init__, register, set_usage_sink, providers_for.
- `apps/api/makma/ai/validation.py`: Problem, ValidationResult, ProjectFacts, parse_payload, _finite, _check_value, validate_operation, _check_constraints, validate_plan, safety_problems, __str__, ok, repair_brief, from_document.
- `apps/api/makma/ai/__init__.py`: source/module definitions inspected.
- `apps/api/makma/ai/providers/anthropic_provider.py`: AnthropicProvider, __init__, available, _get_client, reason, vision.
- `apps/api/makma/ai/providers/openai_provider.py`: OpenAICompatibleProvider, __init__, available, reason.
- `apps/api/makma/ai/providers/__init__.py`: source/module definitions inspected.

## Request routes

| Source | Method | Path | Handler |
|---|---|---|---|
| `apps/api/makma/main.py` | get | `/` | `root` |
| `apps/api/makma/api/routes_ai.py` | post | `/chat` | `chat` |
| `apps/api/makma/api/routes_ai.py` | post | `/apply` | `apply` |
| `apps/api/makma/api/routes_ai.py` | get | `/messages` | `messages` |
| `apps/api/makma/api/routes_ai.py` | delete | `/messages` | `clear_messages` |
| `apps/api/makma/api/routes_ai.py` | get | `/usage` | `usage` |
| `apps/api/makma/api/routes_assets.py` | get | `/projects/{project_id}/assets` | `list_assets` |
| `apps/api/makma/api/routes_assets.py` | post | `/projects/{project_id}/assets` | `upload_asset` |
| `apps/api/makma/api/routes_assets.py` | get | `/projects/{project_id}/assets/{asset_id}` | `get_asset` |
| `apps/api/makma/api/routes_assets.py` | get | `/projects/{project_id}/assets/{asset_id}/usage` | `asset_usage` |
| `apps/api/makma/api/routes_assets.py` | delete | `/projects/{project_id}/assets/{asset_id}` | `delete_asset` |
| `apps/api/makma/api/routes_assets.py` | post | `/projects/{project_id}/assets/{asset_id}/reanalyze` | `reanalyze` |
| `apps/api/makma/api/routes_assets.py` | get | `/media/{key:path}` | `serve_media` |
| `apps/api/makma/api/routes_jobs.py` | post | `/projects/{project_id}/render` | `render` |
| `apps/api/makma/api/routes_jobs.py` | post | `/projects/{project_id}/export` | `export` |
| `apps/api/makma/api/routes_jobs.py` | get | `/projects/{project_id}/render/estimate` | `render_estimate` |
| `apps/api/makma/api/routes_jobs.py` | get | `/projects/{project_id}/health` | `project_health` |
| `apps/api/makma/api/routes_jobs.py` | get | `/projects/{project_id}/jobs` | `list_jobs` |
| `apps/api/makma/api/routes_jobs.py` | get | `/jobs/{job_id}` | `get_job` |
| `apps/api/makma/api/routes_jobs.py` | post | `/jobs/{job_id}/cancel` | `cancel_job` |
| `apps/api/makma/api/routes_jobs.py` | get | `/jobs/{job_id}/stream` | `stream_job` |
| `apps/api/makma/api/routes_projects.py` | post | `/signup` | `signup` |
| `apps/api/makma/api/routes_projects.py` | post | `/signin` | `signin` |
| `apps/api/makma/api/routes_projects.py` | get | `/me` | `me` |
| `apps/api/makma/api/routes_projects.py` | patch | `/me/preferences` | `update_preferences` |
| `apps/api/makma/api/routes_projects.py` | get | `` | `list_projects` |
| `apps/api/makma/api/routes_projects.py` | post | `` | `create_project` |
| `apps/api/makma/api/routes_projects.py` | get | `/{project_id}` | `get_one` |
| `apps/api/makma/api/routes_projects.py` | patch | `/{project_id}` | `update_project` |
| `apps/api/makma/api/routes_projects.py` | delete | `/{project_id}` | `delete_project` |
| `apps/api/makma/api/routes_projects.py` | put | `/{project_id}/document` | `save_document` |
| `apps/api/makma/api/routes_projects.py` | post | `/{project_id}/operations` | `apply_ops` |
| `apps/api/makma/api/routes_projects.py` | post | `/{project_id}/undo` | `undo_edit` |
| `apps/api/makma/api/routes_projects.py` | post | `/{project_id}/redo` | `redo_edit` |
| `apps/api/makma/api/routes_projects.py` | get | `/{project_id}/versions` | `list_versions` |
| `apps/api/makma/api/routes_projects.py` | post | `/{project_id}/versions/{version_id}/restore` | `restore_version` |
| `apps/api/makma/api/routes_system.py` | get | `/capabilities` | `capabilities` |
| `apps/api/makma/api/routes_system.py` | get | `/effects` | `effects` |
| `apps/api/makma/api/routes_system.py` | get | `/health` | `health` |

Static parsing found the modules/handlers above without importing/executing them. Fixtures, enums and abstract declarations are not proof that every flow is complete. See TECHNICAL_ISSUES.md for incomplete adapters and inconsistent pipeline contracts.
