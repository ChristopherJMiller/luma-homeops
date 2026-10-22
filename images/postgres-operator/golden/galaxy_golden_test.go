// galaxy patch — luma-homeops docs/postgres.md
//
// Build gate for images/postgres-operator. Not a patch: the Dockerfile copies
// this file into pkg/cluster/ twice. First on the UNPATCHED v1.11.0 source
// with -galaxy.update, which writes one golden StatefulSet per fixture, then
// after the patches are applied, which compares against those files. A patch
// that changes the StatefulSet of a CR that has not opted in to Patroni REST
// auth would roll that cluster on deploy, so it fails the build here instead.
//
// Use only symbols that exist in unpatched v1.11.0. Package-level names carry
// a galaxy prefix because upstream's tests already define logger and
// eventRecorder.
//
// Run: GALAXY_GOLDEN_DIR=<dir> go test ./pkg/cluster/ -run TestGalaxyGoldenStatefulSets [-args -galaxy.update]

package cluster

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/sirupsen/logrus"
	acidv1 "github.com/zalando/postgres-operator/pkg/apis/acid.zalan.do/v1"
	"github.com/zalando/postgres-operator/pkg/util/config"
	"github.com/zalando/postgres-operator/pkg/util/k8sutil"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
)

var galaxyUpdate = flag.Bool("galaxy.update", false, "write the galaxy golden StatefulSets instead of comparing with them")

var (
	galaxyLogger   = logrus.New().WithField("test", "galaxy-golden")
	galaxyRecorder = record.NewFakeRecorder(100)
)

// The stock Spilo the chart runs (the operator's docker_image).
const galaxySpiloImage = "ghcr.io/zalando/spilo-16:3.2-p3@sha256:b59493aaba6e35c017f22954fa09548060616c8669c58ab2b368689f0ee21884"

// acid-royaltracker's CR shape (cluster/royaltracker/psql.yaml), without env.
const galaxyPlainSpec = `{
  "enableLogicalBackup": true,
  "databases": {"royaltracker": "royaltracker"},
  "numberOfInstances": 2,
  "spiloRunAsUser": 101,
  "spiloRunAsGroup": 103,
  "spiloFSGroup": 103,
  "postgresql": {"version": "16", "parameters": {"synchronous_commit": "off"}},
  "patroni": {"failsafe_mode": true},
  "teamId": "acid",
  "users": {"royaltracker": ["superuser", "createdb"]},
  "volume": {"size": "5Gi", "storageClass": "rook-ceph-block-ssd"},
  "resources": {"requests": {"cpu": "50m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}}
}`

// acid-chrismillerxyz's CR shape (cluster/chrismillerxyz/psql.yaml): exporter
// sidecar, a pg_hba override and parameters.
const galaxySidecarHbaSpec = `{
  "enableLogicalBackup": true,
  "databases": {"postgres": "postgres"},
  "numberOfInstances": 2,
  "spiloRunAsUser": 101,
  "spiloRunAsGroup": 103,
  "spiloFSGroup": 103,
  "postgresql": {"version": "16", "parameters": {"synchronous_commit": "off"}},
  "teamId": "acid",
  "resources": {"requests": {"memory": "256Mi", "cpu": "100m"}, "limits": {"memory": "256Mi", "cpu": "100m"}},
  "users": {"postgres": []},
  "volume": {"size": "3Gi", "storageClass": "rook-ceph-block-ssd"},
  "patroni": {
    "failsafe_mode": true,
    "pg_hba": [
      "host      replication  standby  all  md5",
      "local     all  all  trust",
      "host      all  all  all  trust",
      "hostnossl all  all  all  trust"
    ]
  },
  "sidecars": [{
    "name": "postgres-exporter",
    "image": "quay.io/prometheuscommunity/postgres-exporter:v0.20.1@sha256:ac5ec343104fae0e2d84a27bb8d69b38430a11910c5382cad85d478d2bab713e",
    "ports": [{"name": "metrics", "containerPort": 9187, "protocol": "TCP"}],
    "env": [
      {"name": "DATA_SOURCE_URI", "value": "localhost:5432/postgres?sslmode=disable"},
      {"name": "DATA_SOURCE_USER", "value": "postgres"},
      {"name": "DATA_SOURCE_PASS", "valueFrom": {"secretKeyRef": {"name": "postgres.acid-chrismillerxyz.credentials.postgresql.acid.zalan.do", "key": "password"}}},
      {"name": "PG_EXPORTER_AUTO_DISCOVER_DATABASES", "value": "true"},
      {"name": "PG_EXPORTER_EXTEND_QUERY_PATH", "value": "/etc/postgres_exporter/queries.yaml"}
    ],
    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "200m", "memory": "256Mi"}}
  }]
}`

// CR env blocks that must NOT opt in: only the exact pair of secretKeyRefs to
// patroni-restapi does.
const (
	galaxyEnvOther = `[{"name": "FOO", "value": "bar"}]`

	galaxyEnvUsernameOnly = `[
  {"name": "PATRONI_RESTAPI_USERNAME", "valueFrom": {"secretKeyRef": {"name": "patroni-restapi", "key": "username"}}}
]`

	galaxyEnvPlainValues = `[
  {"name": "PATRONI_RESTAPI_USERNAME", "value": "patroni"},
  {"name": "PATRONI_RESTAPI_PASSWORD", "value": "not-a-secret"}
]`

	galaxyEnvOtherSecret = `[
  {"name": "PATRONI_RESTAPI_USERNAME", "valueFrom": {"secretKeyRef": {"name": "foo", "key": "username"}}},
  {"name": "PATRONI_RESTAPI_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "foo", "key": "password"}}}
]`
)

func galaxyDecode(t *testing.T, what, doc string, into interface{}) {
	t.Helper()
	dec := json.NewDecoder(strings.NewReader(doc))
	dec.DisallowUnknownFields()
	if err := dec.Decode(into); err != nil {
		t.Fatalf("%s: could not decode fixture: %v", what, err)
	}
}

func galaxySpec(t *testing.T, name, specDoc, envDoc string) acidv1.PostgresSpec {
	t.Helper()
	var spec acidv1.PostgresSpec
	galaxyDecode(t, name, specDoc, &spec)
	if envDoc != "" {
		galaxyDecode(t, name, envDoc, &spec.Env)
	}
	return spec
}

func galaxyCluster(spec acidv1.PostgresSpec) *Cluster {
	spiloRunAsUser := int64(101)
	spiloRunAsGroup := int64(103)
	spiloFSGroup := int64(103)
	return New(
		Config{
			OpConfig: config.Config{
				PodManagementPolicy: "ordered_ready",
				ProtectedRoles:      []string{"admin"},
				Auth: config.Auth{
					SuperUsername:       "postgres",
					ReplicationUsername: "standby",
				},
				Resources: config.Resources{
					ClusterNameLabel: "galaxy",
					PodRoleLabel:     "spilo-role",
					SpiloRunAsUser:   &spiloRunAsUser,
					SpiloRunAsGroup:  &spiloRunAsGroup,
					SpiloFSGroup:     &spiloFSGroup,
				},
				EnablePgVersionEnvVar: true,
				EnableReadinessProbe:  true,
				DockerImage:           galaxySpiloImage,
			},
		},
		k8sutil.KubernetesClient{},
		acidv1.Postgresql{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "acid-royaltracker",
				Namespace: "royaltracker",
				UID:       "6f1d2c3b-4a5e-4f60-8b7a-9c0d1e2f3a4b",
			},
			Spec: spec,
		},
		galaxyLogger,
		galaxyRecorder,
	)
}

// galaxyDiffAt returns the offset of the first byte where a and b differ.
func galaxyDiffAt(a, b []byte) int {
	n := len(a)
	if len(b) < n {
		n = len(b)
	}
	for i := 0; i < n; i++ {
		if a[i] != b[i] {
			return i
		}
	}
	return n
}

// galaxyWindow returns up to 200 bytes of b around offset.
func galaxyWindow(b []byte, offset int) string {
	start := offset - 100
	if start < 0 {
		start = 0
	}
	end := start + 200
	if end > len(b) {
		end = len(b)
	}
	if start > end {
		start = end
	}
	return string(b[start:end])
}

func TestGalaxyGoldenStatefulSets(t *testing.T) {
	dir := os.Getenv("GALAXY_GOLDEN_DIR")
	if dir == "" {
		t.Fatal("GALAXY_GOLDEN_DIR is not set; this gate never passes by skipping")
	}

	fixtures := []struct {
		name, spec, env string
	}{
		{"plain", galaxyPlainSpec, ""},
		{"sidecar-hba", galaxySidecarHbaSpec, ""},
		{"env-other", galaxyPlainSpec, galaxyEnvOther},
		{"env-username-only", galaxyPlainSpec, galaxyEnvUsernameOnly},
		{"env-plain-values", galaxyPlainSpec, galaxyEnvPlainValues},
		{"env-other-secret", galaxyPlainSpec, galaxyEnvOtherSecret},
	}

	if *galaxyUpdate {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatalf("could not create %s: %v", dir, err)
		}
	}

	for _, fx := range fixtures {
		t.Run(fx.name, func(t *testing.T) {
			spec := galaxySpec(t, fx.name, fx.spec, fx.env)
			sts, err := galaxyCluster(spec).generateStatefulSet(&spec)
			if err != nil {
				t.Fatalf("generateStatefulSet: %v", err)
			}
			got, err := json.Marshal(sts)
			if err != nil {
				t.Fatalf("could not marshal the StatefulSet: %v", err)
			}

			path := filepath.Join(dir, fx.name+".json")
			if *galaxyUpdate {
				if err := os.WriteFile(path, got, 0o644); err != nil {
					t.Fatalf("could not write %s: %v", path, err)
				}
				t.Logf("galaxy golden: wrote %s (%d bytes)", path, len(got))
				return
			}

			want, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("could not read golden %s: %v", path, err)
			}
			if !bytes.Equal(got, want) {
				at := galaxyDiffAt(want, got)
				t.Fatalf("galaxy golden: %s differs from the unpatched StatefulSet at byte %d (golden %d bytes, got %d)\ngolden: %s\n   got: %s",
					fx.name, at, len(want), len(got), galaxyWindow(want, at), galaxyWindow(got, at))
			}
			t.Logf("galaxy golden: %s byte-identical (%d bytes)", fx.name, len(got))
		})
	}
}
