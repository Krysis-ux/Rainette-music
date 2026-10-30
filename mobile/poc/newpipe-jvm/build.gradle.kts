plugins { application }

repositories {
    mavenCentral()
    maven("https://jitpack.io")
}

dependencies {
    // Pinned: the extractor is the thing most likely to change under us.
    implementation("com.github.TeamNewPipe:NewPipeExtractor:v0.26.5")
}

java { toolchain { languageVersion.set(JavaLanguageVersion.of(21)) } }

application { mainClass.set("poc.Main") }
