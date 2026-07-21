<?php
/**
 * ToolHub 공통 행위 로그 — PHP 참조 구현 (stage 대시보드 등 PHP 서비스용).
 * 공통 규약(docs/toolhub-audit-contract.md)을 따른다. SERVICE만 자기 것으로 바꾼다.
 *
 * 사용:
 *   require_once 'audit.php';
 *   Audit::$service = 'stage';                 // 또는 env TOOLHUB_SERVICE
 *   Audit::login($_SERVER['REMOTE_USER']);     // SSO 접근 시(세션 스로틀)
 *   Audit::record($user, 'build_trigger', 'buildid=123', 'ok');
 *   $rows = Audit::recent(200, null, 'login');
 */
class Audit {
    public static $service = null;   // 미설정 시 env TOOLHUB_SERVICE
    public static $dbPath  = null;   // 미설정 시 env TOOLHUB_AUDIT_DB
    public static $throttleSec = 1800;   // 로그인 세션 창(기본 30분)

    private static function db(): PDO {
        $path = self::$dbPath ?: (getenv('TOOLHUB_AUDIT_DB')
                                  ?: '/opt/toolhub/data/action_log.db');
        $pdo = new PDO('sqlite:' . $path);
        $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $pdo->exec('CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, service TEXT NOT NULL,
            action TEXT NOT NULL, target TEXT, result TEXT, detail TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime(\'now\')))');
        return $pdo;
    }

    private static function service(): string {
        return self::$service ?: (getenv('TOOLHUB_SERVICE') ?: 'stage');
    }

    /** 행위 1건 기록. 실패해도 서비스 흐름을 막지 않는다. */
    public static function record($user, $action, $target = null,
                                  $result = null, $detail = null): void {
        try {
            $st = self::db()->prepare(
                'INSERT INTO action_log (user_id, service, action, target, result, detail)
                 VALUES (?,?,?,?,?,?)');
            $st->execute([$user, self::service(), $action, $target, $result, $detail]);
        } catch (Exception $e) { /* 이력 실패가 본 흐름을 막지 않음 */ }
    }

    /** SSO 접근 시. 세션 창 내 재호출은 기록하지 않는다(세션 기반 스로틀). */
    public static function login($user): void {
        if (session_status() !== PHP_SESSION_ACTIVE) { @session_start(); }
        $now = time();
        $key = '_audit_last_login';
        $last = $_SESSION[$key] ?? null;
        if ($last !== null && ($now - $last) < self::$throttleSec) { return; }
        $_SESSION[$key] = $now;
        self::record($user, 'login', null, $last === null ? 'new' : 'resume');
    }

    public static function recent($limit = 200, $user = null, $action = null): array {
        $cl = []; $args = [];
        if ($user)   { $cl[] = 'user_id=?'; $args[] = $user; }
        if ($action) { $cl[] = 'action=?';  $args[] = $action; }
        $where = $cl ? (' WHERE ' . implode(' AND ', $cl)) : '';
        $args[] = (int)$limit;
        $st = self::db()->prepare(
            "SELECT * FROM action_log$where ORDER BY id DESC LIMIT ?");
        $st->execute($args);
        return $st->fetchAll(PDO::FETCH_ASSOC);
    }
}
