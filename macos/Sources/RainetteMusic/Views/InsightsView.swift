import Charts
import SwiftUI

@available(macOS 14, *)
struct InsightsView: View {
    @Environment(LibraryModel.self) private var library
    @State private var days = 7
    @State private var insights: Insights?
    @State private var loading = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                PageHeader(title: "Insights", subtitle: "What you actually played, \(windowLabel).")
                if loading && insights == nil {
                    HStack { Spacer(); ProgressView().controlSize(.small); Spacer() }.frame(minHeight: 200)
                } else if let insights, insights.totalPlays > 0 {
                    content(insights)
                } else {
                    EmptyState(title: "No plays \(windowLabel)", symbol: "chart.bar.xaxis",
                               message: "Play a few songs and this page starts telling your listening story.")
                }
            }
            .padding(CardMetrics.pagePadding)
        }
        .navigationTitle("Insights")
        .toolbar {
            ToolbarItem(placement: .principal) {
                Picker("Range", selection: $days) {
                    Text("7 Days").tag(7); Text("30 Days").tag(30); Text("All Time").tag(0)
                }
                .pickerStyle(.segmented).frame(width: 240)
            }
        }
        .task(id: days) {
            loading = true
            let loaded = await library.insights(days: days)
            withAnimation(.smooth) { insights = loaded; loading = false }
        }
    }

    private var windowLabel: String { days == 7 ? "in the last 7 days" : days == 30 ? "in the last 30 days" : "of all time" }

    @ViewBuilder
    private func content(_ insights: Insights) -> some View {
        HStack(spacing: 14) {
            stat("\(insights.totalPlays)", insights.totalPlays == 1 ? "play" : "plays", "play.circle.fill")
            stat(minutes(insights.totalMinutes), "listened", "clock.fill")
            stat("\(insights.uniqueTracks)", insights.uniqueTracks == 1 ? "song" : "songs", "music.note")
            stat("\(insights.uniqueArtists)", insights.uniqueArtists == 1 ? "artist" : "artists", "music.mic")
        }
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "Your Rhythm", subtitle: "Plays per \(insights.unit)")
            Chart(insights.buckets) { bucket in
                BarMark(x: .value("When", bucket.start, unit: unit(insights.unit)), y: .value("Plays", bucket.count))
                    .foregroundStyle(Color.accentColor.gradient)
                    .cornerRadius(4)
            }
            .chartYAxis { AxisMarks(position: .leading) }
            .frame(height: 200)
            .padding(18)
            .glassSurface(in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        }
        if !insights.topTracks.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                SectionHeader(title: "Heavy Rotation")
                LazyVStack(spacing: 2) {
                    ForEach(Array(insights.topTracks.prefix(10).enumerated()), id: \.element.id) { offset, track in
                        TrackRow(track: track, list: insights.topTracks, number: offset + 1)
                    }
                }
            }
        }
        if !insights.topArtists.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                SectionHeader(title: "Top Artists")
                LazyVGrid(columns: CardMetrics.gridColumns, spacing: CardMetrics.gridSpacing) {
                    ForEach(insights.topArtists) { ArtistCard(artist: $0, detail: Format.count($0.playCount, "play")) }
                }
            }
        }
    }

    private func stat(_ value: String, _ label: String, _ symbol: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: symbol).font(.system(size: 16)).foregroundStyle(Color.accentColor)
            Text(value).font(.system(size: 28, weight: .bold).monospacedDigit())
            Text(label).font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .glassSurface(in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private func minutes(_ value: Double) -> String {
        value >= 60 ? String(format: "%.1fh", value / 60) : "\(Int(value))m"
    }

    private func unit(_ name: String) -> Calendar.Component {
        switch name { case "week": return .weekOfYear; case "month": return .month; default: return .day }
    }
}
