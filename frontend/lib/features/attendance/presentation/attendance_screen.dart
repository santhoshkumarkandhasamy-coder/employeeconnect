import 'package:flutter/material.dart';

import 'calendar_tab.dart';
import 'history_tab.dart';
import 'today_tab.dart';

/// Attendance tab: Today (check in/out) | Calendar | History.
class AttendanceScreen extends StatelessWidget {
  const AttendanceScreen({super.key});

  @override
  Widget build(BuildContext context) => DefaultTabController(
        length: 3,
        child: Scaffold(
          appBar: AppBar(
            title: const Text('Attendance'),
            bottom: const TabBar(
              tabs: [
                Tab(text: 'Today'),
                Tab(text: 'Calendar'),
                Tab(text: 'History'),
              ],
            ),
          ),
          body: const TabBarView(children: [TodayTab(), CalendarTab(), HistoryTab()]),
        ),
      );
}
