import 'dart:io' show Platform;

import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:timezone/data/latest.dart' as tzdata;
import 'package:timezone/timezone.dart' as tz;

import '../../../core/utils/dates.dart';
import '../domain/task_models.dart';
import 'task_repository.dart';

/// Shows task reminders as notifications scheduled ON THE PHONE, so they ring on
/// time even offline and without Firebase. The server also stamps / pushes them
/// (for when push is set up); the phone copy is the one users rely on.
abstract class ReminderScheduler {
  /// Replaces every scheduled reminder with those of [upcoming] (open tasks with
  /// a reminder time still in the future).
  Future<void> sync(List<TaskItem> upcoming);

  /// Removes all scheduled reminders (logout / account wipe).
  Future<void> clear();
}

class NoReminderScheduler implements ReminderScheduler {
  const NoReminderScheduler();
  @override
  Future<void> sync(List<TaskItem> upcoming) async {}
  @override
  Future<void> clear() async {}
}

/// Max reminders kept scheduled (iOS allows 64 pending; Android has no small limit).
const int maxScheduledReminders = 60;

/// Open tasks whose reminder is still ahead, soonest first, capped.
List<TaskItem> remindersToSchedule(List<TaskItem> items, DateTime now) {
  final out = [
    for (final t in items)
      if (!t.done && t.remindAt != null && t.remindAt!.isAfter(now)) t,
  ]..sort((a, b) => a.remindAt!.compareTo(b.remindAt!));
  return out.take(maxScheduledReminders).toList();
}

/// Stable notification id for a task (UUID string -> positive 31-bit int).
int reminderId(String taskId) {
  var h = 0;
  for (final c in taskId.codeUnits) {
    h = (h * 31 + c) & 0x7fffffff;
  }
  return h;
}

class LocalReminderScheduler implements ReminderScheduler {
  LocalReminderScheduler([FlutterLocalNotificationsPlugin? plugin])
      : _plugin = plugin ?? FlutterLocalNotificationsPlugin();
  final FlutterLocalNotificationsPlugin _plugin;
  Future<void>? _ready;

  Future<void> _init() => _ready ??= () async {
        tzdata.initializeTimeZones();
        await _plugin.initialize(
          settings: const InitializationSettings(
            android: AndroidInitializationSettings('@mipmap/ic_launcher'),
            iOS: DarwinInitializationSettings(),
          ),
        );
        // Android 13+: ask once for permission to show notifications.
        await _plugin
            .resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>()
            ?.requestNotificationsPermission();
      }();

  static const _details = NotificationDetails(
    android: AndroidNotificationDetails(
      'task_reminders',
      'Task reminders',
      channelDescription: 'Reminders for your tasks in Employee Connect',
      importance: Importance.high,
      priority: Priority.high,
    ),
    iOS: DarwinNotificationDetails(),
  );

  @override
  Future<void> sync(List<TaskItem> upcoming) async {
    await _init();
    await _plugin.cancelAll();
    for (final t in remindersToSchedule(upcoming, DateTime.now())) {
      final due = t.dueAt.toLocal();
      await _plugin.zonedSchedule(
        id: reminderId(t.id),
        scheduledDate: tz.TZDateTime.from(t.remindAt!.toUtc(), tz.UTC),
        title: 'Reminder: ${t.title}',
        body: 'Due ${formatDayLong(due)} at ${formatTime(due)}',
        notificationDetails: _details,
        // Inexact: needs no "alarms" permission; Android may deliver a few minutes late in deep sleep.
        androidScheduleMode: AndroidScheduleMode.inexactAllowWhileIdle,
      );
    }
  }

  @override
  Future<void> clear() async {
    await _init();
    await _plugin.cancelAll();
  }
}

final reminderSchedulerProvider = Provider<ReminderScheduler>(
  (ref) => !kIsWeb && (Platform.isAndroid || Platform.isIOS) ? LocalReminderScheduler() : const NoReminderScheduler(),
);

/// Days ahead whose reminders are kept on the phone (the API allows 92-day ranges).
const int reminderWindowDays = 60;

/// Fetches the user's open tasks for the next [reminderWindowDays] days and
/// re-schedules their reminders. Best effort: failures leave the old schedule.
Future<void> syncTaskReminders(Ref ref) async {
  try {
    final now = DateTime.now();
    final items = await ref
        .read(taskRepositoryProvider)
        .list(from: ymd(now), to: ymd(now.add(const Duration(days: reminderWindowDays))));
    await ref.read(reminderSchedulerProvider).sync(items);
  } on Object {
    // offline or no permission: keep whatever was scheduled before
  }
}

/// Watch (or read) this to (re)run the sync; invalidate it after task changes.
final taskReminderSyncProvider = FutureProvider.autoDispose<void>((ref) => syncTaskReminders(ref));
