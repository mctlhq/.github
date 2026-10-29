package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv/taskstore"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// pgStore implements taskstore.Store over pgx. a2a-go v2.7.0 ships only
// taskstore.InMemory (a2asrv/taskstore/inmemory.go); this is the smallest
// honest Postgres port of it: same OCC contract (PrevVersion ->
// ErrConcurrentModification), same per-user ownership masking (foreign task
// -> ErrTaskNotFound), plus three helpers the SDK has no hook for
// (contextOwner, taskForMessage, patchMetadata).
type pgStore struct {
	pool *pgxpool.Pool
	auth taskstore.Authenticator
}

const schema = `
CREATE TABLE IF NOT EXISTS tasks (
	id         text PRIMARY KEY,
	context_id text NOT NULL,
	user_name  text NOT NULL,
	version    bigint NOT NULL,
	task       jsonb NOT NULL,
	updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tasks_context ON tasks (context_id, user_name);
CREATE TABLE IF NOT EXISTS task_messages (
	message_id text NOT NULL,
	user_name  text NOT NULL,
	task_id    text NOT NULL REFERENCES tasks(id),
	PRIMARY KEY (message_id, user_name)
);`

func newPGStore(ctx context.Context, dsn string, auth taskstore.Authenticator) (*pgStore, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, err
	}
	if _, err := pool.Exec(ctx, schema); err != nil {
		pool.Close()
		return nil, fmt.Errorf("schema: %w", err)
	}
	return &pgStore{pool: pool, auth: auth}, nil
}

func (s *pgStore) Close() { s.pool.Close() }

func (s *pgStore) user(ctx context.Context) (string, error) {
	u, err := s.auth(ctx)
	if err != nil {
		return "", fmt.Errorf("taskstore auth failed: %w", err)
	}
	return u, nil
}

func (s *pgStore) Create(ctx context.Context, task *a2a.Task) (taskstore.TaskVersion, error) {
	user, err := s.user(ctx)
	if err != nil {
		return taskstore.TaskVersionMissing, err
	}
	body, err := json.Marshal(task)
	if err != nil {
		return taskstore.TaskVersionMissing, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return taskstore.TaskVersionMissing, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck // no-op after Commit
	_, err = tx.Exec(ctx, `INSERT INTO tasks (id, context_id, user_name, version, task) VALUES ($1,$2,$3,1,$4)`,
		string(task.ID), task.ContextID, user, body)
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		return taskstore.TaskVersionMissing, taskstore.ErrTaskAlreadyExists
	}
	if err != nil {
		return taskstore.TaskVersionMissing, err
	}
	if err := indexMessages(ctx, tx, task, user); err != nil {
		return taskstore.TaskVersionMissing, err
	}
	return 1, tx.Commit(ctx)
}

func (s *pgStore) Update(ctx context.Context, req *taskstore.UpdateRequest) (taskstore.TaskVersion, error) {
	user, err := s.user(ctx)
	if err != nil {
		return taskstore.TaskVersionMissing, err
	}
	body, err := json.Marshal(req.Task)
	if err != nil {
		return taskstore.TaskVersionMissing, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return taskstore.TaskVersionMissing, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	var owner string
	var version int64
	err = tx.QueryRow(ctx, `SELECT user_name, version FROM tasks WHERE id=$1 FOR UPDATE`, string(req.Task.ID)).Scan(&owner, &version)
	if errors.Is(err, pgx.ErrNoRows) || (err == nil && owner != user) {
		return taskstore.TaskVersionMissing, a2a.ErrTaskNotFound
	}
	if err != nil {
		return taskstore.TaskVersionMissing, err
	}
	if req.PrevVersion != taskstore.TaskVersionMissing && taskstore.TaskVersion(version) != req.PrevVersion {
		return taskstore.TaskVersionMissing, taskstore.ErrConcurrentModification
	}
	next := version + 1
	if _, err := tx.Exec(ctx, `UPDATE tasks SET task=$2, version=$3, updated_at=now() WHERE id=$1`, string(req.Task.ID), body, next); err != nil {
		return taskstore.TaskVersionMissing, err
	}
	if err := indexMessages(ctx, tx, req.Task, user); err != nil {
		return taskstore.TaskVersionMissing, err
	}
	return taskstore.TaskVersion(next), tx.Commit(ctx)
}

func indexMessages(ctx context.Context, tx pgx.Tx, task *a2a.Task, user string) error {
	for _, m := range task.History {
		if m == nil || m.Role != a2a.MessageRoleUser || m.ID == "" {
			continue
		}
		if _, err := tx.Exec(ctx, `INSERT INTO task_messages (message_id, user_name, task_id) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING`,
			m.ID, user, string(task.ID)); err != nil {
			return err
		}
	}
	return nil
}

func (s *pgStore) Get(ctx context.Context, taskID a2a.TaskID) (*taskstore.StoredTask, error) {
	user, err := s.user(ctx)
	if err != nil {
		return nil, err
	}
	var owner string
	var version int64
	var body []byte
	err = s.pool.QueryRow(ctx, `SELECT user_name, version, task FROM tasks WHERE id=$1`, string(taskID)).Scan(&owner, &version, &body)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, a2a.ErrTaskNotFound
	}
	if err != nil {
		return nil, err
	}
	if owner != user { // mask ownership mismatch as not found (A2A spec 3.3.2), as taskstore.InMemory does
		return nil, a2a.ErrTaskNotFound
	}
	var task a2a.Task
	if err := json.Unmarshal(body, &task); err != nil {
		return nil, fmt.Errorf("decode task %s: %w", taskID, err)
	}
	return &taskstore.StoredTask{Task: &task, Version: taskstore.TaskVersion(version), User: owner}, nil
}

// List supports the filters the executor needs (owner + contextId) and the
// history/artifact trimming of the in-memory store; page tokens are refused
// rather than silently ignored.
func (s *pgStore) List(ctx context.Context, req *a2a.ListTasksRequest) (*a2a.ListTasksResponse, error) {
	user, err := s.user(ctx)
	if user == "" || err != nil {
		return nil, a2a.ErrUnauthenticated
	}
	if req.PageToken != "" {
		return nil, fmt.Errorf("page tokens not implemented: %w", a2a.ErrUnsupportedOperation)
	}
	pageSize := req.PageSize
	if pageSize == 0 {
		pageSize = 50
	}
	rows, err := s.pool.Query(ctx,
		`SELECT task FROM tasks WHERE user_name=$1 AND ($2='' OR context_id=$2) ORDER BY updated_at ASC, id ASC LIMIT $3`,
		user, req.ContextID, pageSize)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*a2a.Task
	for rows.Next() {
		var body []byte
		if err := rows.Scan(&body); err != nil {
			return nil, err
		}
		var t a2a.Task
		if err := json.Unmarshal(body, &t); err != nil {
			return nil, err
		}
		if req.Status != a2a.TaskStateUnspecified && t.Status.State != req.Status {
			continue
		}
		if req.HistoryLength != nil && *req.HistoryLength >= 0 && len(t.History) > *req.HistoryLength {
			t.History = t.History[len(t.History)-*req.HistoryLength:]
		}
		if !req.IncludeArtifacts {
			t.Artifacts = nil
		}
		out = append(out, &t)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return &a2a.ListTasksResponse{Tasks: out, TotalSize: len(out), PageSize: pageSize}, nil
}

// contextOwner deliberately ignores the caller: it answers "who owns this
// context" so the interceptor can refuse a foreign continuation.
func (s *pgStore) contextOwner(ctx context.Context, contextID string) (string, bool, error) {
	var owner string
	err := s.pool.QueryRow(ctx, `SELECT user_name FROM tasks WHERE context_id=$1 LIMIT 1`, contextID).Scan(&owner)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", false, nil
	}
	return owner, err == nil, err
}

// taskForMessage is the messageId idempotency index, scoped to the caller.
func (s *pgStore) taskForMessage(ctx context.Context, messageID string) (a2a.TaskID, bool, error) {
	user, err := s.user(ctx)
	if err != nil {
		return "", false, err
	}
	var id string
	err = s.pool.QueryRow(ctx, `SELECT task_id FROM task_messages WHERE message_id=$1 AND user_name=$2`, messageID, user).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", false, nil
	}
	return a2a.TaskID(id), err == nil, err
}

// patchMetadata writes one metadata key directly. The SDK refuses any event
// on a terminal task (internal/taskupdate/manager.go Process), so a loop that
// learns about a cancel after the fact has no sanctioned way to record it;
// this is the documented bypass, version-bumped so OCC stays honest.
func (s *pgStore) patchMetadata(ctx context.Context, taskID a2a.TaskID, key string, value any) error {
	user, err := s.user(ctx)
	if err != nil {
		return err
	}
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	tag, err := s.pool.Exec(ctx, `UPDATE tasks
		SET task = jsonb_set(CASE WHEN task ? 'metadata' THEN task ELSE task || '{"metadata":{}}'::jsonb END, ARRAY['metadata',$3::text], $4::jsonb, true),
		    version = version + 1, updated_at = now()
		WHERE id=$1 AND user_name=$2`, string(taskID), user, key, raw)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return a2a.ErrTaskNotFound
	}
	return nil
}
